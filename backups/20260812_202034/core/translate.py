import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    GLOSSARY_PATH,
    TRANSLATIONS_PATH,
    resolve_segments_path,
    resolve_api_key,
    video_output_dir,
)
from .ocr import load_segments

PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
}

SYSTEM_PROMPT_DIALOGUE = (
    "你是专业的日语字幕翻译。把日语台词翻译成简体中文。"
    "要求：1) 只输出译文，不要任何解释、注释、引号或编号；"
    "2) 翻译自然口语化，符合中文表达习惯；"
    "3) 保留原文的换行结构；"
    "4) 文本中用 ⟨⟨数字⟩⟩ 标注的词语是已确定的译名，必须原样保留该符号，不要翻译或改动。"
)

SYSTEM_PROMPT_RETRANSLATE = (
    "你是专业的日语字幕翻译。上次翻译后以下日文台词仍残留日语，请重新翻译成简体中文。"
    "硬性要求：1) 译文必须全部是简体中文，禁止出现任何日文假名（平假名、片假名）和长音符「ー」；"
    "2) 专有名词（人名、机构名、作品名等）可以音译或用括号保留原文，但整句话必须是中文；"
    "3) 只输出译文，不要任何解释、注释、引号或编号；"
    "4) 文本中用 ⟨⟨数字⟩⟩ 标注的词语是已确定的译名，必须原样保留该符号，不要翻译或改动。"
)


SYSTEM_PROMPT_SCRIPT = (
    "你是专业的日语字幕翻译。下面是一段日文台词剧本，每一行格式为：\n"
    "[行号] 日文原文\n"
    "请把每一行翻译成简体中文。\n"
    "输出格式必须为（行号、顺序、行数必须与输入完全一致）：\n"
    "[行号] 中文译文\n"
    "要求：\n"
    "1) 只输出译文列表，不要任何解释、注释、标题或额外内容；\n"
    "2) 翻译自然口语化，符合中文表达习惯，保留原文的语气与换行结构；\n"
    "3) 人名、专有名词按下方词典翻译；词典未覆盖的交给你的判断；\n"
    "4) 每一行都必须输出译文，不能合并、跳过或自行拆分行。\n"
)

_SCRIPT_LINE_RE = re.compile(r"^\s*\[(\d+)\]\s*(.+?)\s*$")

GLOSSARY_TEMPLATE = """\
{
  "names": {},
  "proper_nouns": {},
  "_说明": "names=人名，proper_nouns=专有名词。台词中出现的这些词会按词典译名直接替换，不被模型改写。填好后再跑 translate 即生效。"
}
"""

FORMAT_SPEC = (
    "本文件格式：每条记录为「原文 -> {translation, first_seen, last_seen}」，"
    "translation=中文译文，first_seen/last_seen=该原文首次/最后出现的时间(秒)。"
    "修改翻译只改 translation 的值；其余字段为程序生成，请勿改动或删除。"
    "除下划线开头的键外，其余键均为台词原文，不可重命名。"
)

REVIEW_SAMPLE_SIZE = 8


JP_KANA_RE = re.compile(r"[\u3041-\u3096\u30A1-\u30FA\u30FC]")  # 平假名/片假名/长音符


def _looks_garbled(t):
    return "\ufffd" in t or bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", t))


def scan_translation_quality(data):
    """全量扫描译文，返回可疑条目 [(原文, 译文, 原因)]。

    规则：翻译文本应大部分为中文。译文仍含日文假名（残留日语/未翻译）、
    为空或含乱码/异常字符的条目，均视为可疑。
    """
    suspicious = []
    for source, translated in data.items():
        if str(source).startswith("_"):
            continue
        t = str(translated).strip() if translated else ""
        if not t:
            suspicious.append((source, t, "译文为空"))
        elif _looks_garbled(t):
            suspicious.append((source, t, "译文含乱码/异常字符"))
        elif JP_KANA_RE.search(t):
            suspicious.append((source, t, "译文仍含假名（残留日语/未翻译）"))
    return suspicious


def load_translations(path=None):
    path = path or resolve_translations_path()
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
    out = {}
    for k, v in raw.items():
        key = str(k)
        if key.startswith("_") or key in ("names", "dialogues", "timestamps"):
            continue
        t = v["translation"] if isinstance(v, dict) else v
        t = str(t).strip() if t else ""
        if t:
            out[key] = t
    return out


def save_translations(data, path, dial_seen=None):
    dial_seen = dial_seen or {}
    out = {"_格式说明": FORMAT_SPEC}
    for k in sorted(data):
        t0, t1 = dial_seen.get(k, [0.0, 0.0])
        out[k] = {
            "translation": data[k],
            "first_seen": round(t0, 2),
            "last_seen": round(t1, 2),
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def load_glossary():
    if not os.path.exists(GLOSSARY_PATH):
        with open(GLOSSARY_PATH, "w", encoding="utf-8") as f:
            f.write(GLOSSARY_TEMPLATE)
        return {"names": {}, "proper_nouns": {}}
    try:
        with open(GLOSSARY_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data.setdefault("names", {})
    data.setdefault("proper_nouns", {})
    return data


def glossary_terms(glossary):
    terms = {}
    for section in ("names", "proper_nouns"):
        for k, v in glossary.get(section, {}).items():
            k = str(k).strip()
            v = str(v).strip()
            if k and v and not k.startswith("_"):
                terms[k] = v
    return terms


# 词典词只在其为“独立词”时替换：左邻不是假名/汉字（词首、标点、空格、ASCII 之后）才替换，
# 避免把长词内部的子串误替换（如 決める 里的 める 不应被当成角色名 梅露）。
# ・(30FB)、、(3001)、。(3002)、！？等标点视为边界，不阻止替换。
_WORD_CHAR_LOOKBEHIND = r"(?<![぀-ヺー-ヿ㐀-鿿])"


def _substitute_glossary(text, terms):
    keys = sorted(terms, key=len, reverse=True)
    mapping = {}
    new_text = text
    for i, key in enumerate(keys):
        if key in new_text:
            token = "⟨⟨%d⟩⟩" % i
            new_text = re.sub(_WORD_CHAR_LOOKBEHIND + re.escape(key), token, new_text)
            mapping[token] = terms[key]
    return new_text, mapping


def _restore_glossary(text, mapping):
    for token, value in mapping.items():
        text = text.replace(token, value)
    return re.sub(r"⟨⟨\d+⟩⟩", "", text)


def _make_client(cfg):
    provider = cfg.get("provider") or "deepseek"
    preset = PROVIDERS.get(provider, {})
    if provider == "mock":
        return None, "mock", True
    base_url = cfg.get("base_url") or preset.get("base_url")
    model = cfg.get("model") or preset.get("model")
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=resolve_api_key(cfg), timeout=60)
    return client, model, False


def _translate_one(client, model, text, system_prompt):
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        extra_body={"thinking": {"type": "disabled"}},
    )
    return resp.choices[0].message.content.strip()


def _safe_translate(client, model, text, system_prompt):
    last_err = None
    for attempt in range(3):
        try:
            return _translate_one(client, model, text, system_prompt)
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    print("翻译失败: %r -> %s" % (text, last_err))
    return None


def _first_seen_order(segments):
    """按时间序返回不重复台词的首次出现顺序。"""
    seen, out = set(), []
    for s in segments:
        if s["text"] not in seen:
            seen.add(s["text"])
            out.append(s["text"])
    return out


def _chunk_lines(lines, size):
    """把 [(id, text), ...] 切成连续小块，保留相邻上下文。"""
    size = max(1, int(size or 50))
    return [lines[i:i + size] for i in range(0, len(lines), size)]


def _glossary_block(terms, script_text):
    """把剧本中出现过的词典词拼成系统提示词块（按长度倒序，避免歧义）。"""
    hits = sorted(((k, v) for k, v in terms.items() if k in script_text),
                  key=lambda kv: -len(kv[0]))
    if not hits:
        return ""
    return "\n".join(["词典："] + ["  %s → %s" % (k, v) for k, v in hits])


def _parse_script_response(text):
    """从模型输出解析 [行号] 译文 -> {行号: 译文}。"""
    out = {}
    for ln in (text or "").splitlines():
        m = _SCRIPT_LINE_RE.match(ln)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def _translate_script_chunk(client, model, chunk, glossary_block):
    """翻译一小块剧本，返回 {行号: 译文}。"""
    sys_prompt = SYSTEM_PROMPT_SCRIPT + ("\n" + glossary_block if glossary_block else "")
    body = "\n".join("[%d] %s" % (i, t) for i, t in chunk)
    zh = _safe_translate(client, model, body, sys_prompt)
    if not zh:
        return {}
    return _parse_script_response(zh)


def _print_translation_review_sample(data, sample_size=REVIEW_SAMPLE_SIZE):
    """输出随机原文—译文对，供智能体在翻译完成后检查文本异常。"""
    candidates = sorted((source, translated) for source, translated in data.items() if translated)
    if not candidates:
        return
    selected = random.sample(candidates, min(sample_size, len(candidates)))
    print("翻译抽检：请智能体检查以下随机 %d/%d 条原文—译文对：" % (len(selected), len(candidates)))
    for i, (source, translated) in enumerate(selected, 1):
        print("  [%d] 日：%s" % (i, source))
        print("      中：%s" % translated)
    print("若发现乱码、无意义符号等明显非正常文本，请运行 python run.py translate <视频> --force 重新发起翻译后再检查。")


def run_translate(cfg, force=False, video=None):
    segments_path = resolve_segments_path(video)
    segments = load_segments(segments_path)
    if not segments:
        raise SystemExit("没有待翻译的字幕，请先运行: python run.py ocr <视频>")
    if video:
        translations_path = os.path.join(video_output_dir(video), "translations.json")
    else:
        translations_path = os.path.join(os.path.dirname(segments_path), "translations.json")

    fps = 0.0
    try:
        with open(segments_path, encoding="utf-8") as f:
            fps = json.load(f).get("fps", 0.0) or 0.0
    except Exception:
        pass

    dialogue_texts = sorted({s["text"] for s in segments})
    info = {}
    for s in segments:
        t0, t1 = s["start"], s["end"]
        cur = info.get(s["text"])
        if cur is None:
            info[s["text"]] = [t0, t1]
        else:
            info[s["text"]][0] = min(cur[0], t0)
            info[s["text"]][1] = max(cur[1], t1)
    dial_seen = {k: [a / fps if fps else a, b / fps if fps else b] for k, (a, b) in info.items()}

    tcfg = cfg["translation"]
    client, model, is_mock = _make_client(tcfg)
    data = load_translations(translations_path)
    terms = glossary_terms(load_glossary())

    # --force 会重译所有台词。整行恰好等于词典词的（如纯人名行）直接按词典填，不占位替换。
    todo = [t for t in dialogue_texts if force or not data.get(t)]
    if todo:
        for t in list(todo):
            if terms.get(t):
                data[t] = terms[t]
        todo = [t for t in todo if not terms.get(t)]

    if todo:
        mode = (tcfg.get("mode") or "script").strip().lower()
        print("开始翻译 %d 条台词（%s / %s，模式 %s）..." % (len(todo), tcfg.get("provider"), model, mode))
        if is_mock:
            for t in todo:
                data[t] = "[译] %s" % t
                save_translations(data, translations_path, dial_seen)
        elif mode == "script":
            # 剧本化翻译：按首现时间序拼接成剧本，glossary 作为系统提示词，让模型按上下文翻译
            order = [t for t in _first_seen_order(segments) if t in todo]
            lines = list(enumerate(order))  # (剧本行号, 文本)
            chunks = _chunk_lines(lines, tcfg.get("script_chunk_lines") or 50)
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(chunks)))) as ex:
                future_chunk = {}
                for chunk in chunks:
                    gb = _glossary_block(terms, "\n".join(t for _, t in chunk))
                    future_chunk[ex.submit(_translate_script_chunk, client, model, chunk, gb)] = chunk
                for fut in as_completed(future_chunk):
                    parsed = fut.result() or {}
                    for sid, txt in future_chunk[fut]:
                        if parsed.get(sid):
                            data[txt] = parsed[sid]
            # 逐行兜底：解析失败/缺失的行回落到逐行翻译
            for _sid, txt in lines:
                if not data.get(txt):
                    src_txt, mapping = _substitute_glossary(txt, terms)
                    zh = _safe_translate(client, model, src_txt, SYSTEM_PROMPT_DIALOGUE)
                    if zh:
                        data[txt] = _restore_glossary(zh, mapping)
                    save_translations(data, translations_path, dial_seen)
        else:
            def work(text):
                source, mapping = _substitute_glossary(text, terms)
                zh = _safe_translate(client, model, source, SYSTEM_PROMPT_DIALOGUE)
                return text, (_restore_glossary(zh, mapping) if zh else None)

            with ThreadPoolExecutor(max_workers=6) as ex:
                for text, zh in ex.map(work, todo):
                    if zh:
                        data[text] = zh
                        save_translations(data, translations_path, dial_seen)

    keys = set(dialogue_texts)
    data = {k: v for k, v in data.items() if k in keys}
    save_translations(data, translations_path, dial_seen)

    if os.path.abspath(translations_path) != os.path.abspath(TRANSLATIONS_PATH) and os.path.exists(TRANSLATIONS_PATH):
        try:
            os.remove(TRANSLATIONS_PATH)
        except OSError:
            pass

    missing = [t for t in dialogue_texts if not data.get(t)]
    print("翻译完成：%d 条台词 → %s" % (len(data), translations_path))
    if missing:
        print("以下 %d 条翻译失败，可稍后重跑 translate 补齐：%s" % (len(missing), missing))
    # 全量扫描译文：翻译文本应大部分为中文，发现日语等可疑条目自动重新翻译（最多 2 轮）
    if not is_mock:
        for _round in range(2):
            suspicious = scan_translation_quality(data)
            auto = [s for s, _t, r in suspicious
                    if r.startswith("译文仍含假名") or r.startswith("译文为空")]
            if not auto:
                break
            print("全量扫描发现 %d 条译文可疑（残留日语/为空），自动重新翻译（第 %d 轮）..." % (len(auto), _round + 1))
            for source in auto:
                src_txt, mapping = _substitute_glossary(source, terms)
                zh = _safe_translate(client, model, src_txt, SYSTEM_PROMPT_RETRANSLATE)
                if zh:
                    data[source] = _restore_glossary(zh, mapping)
            save_translations(data, translations_path, dial_seen)

    suspicious = scan_translation_quality(data)
    print("全量扫描完成：%d/%d 条译文可疑%s" % (
        len(suspicious), len(data),
        "" if suspicious else "（全部通过，译文均为中文）"))
    for s, t, r in suspicious:
        print("  [%s] 日：%s" % (r, s))
        print("      中：%s" % t)
    _print_translation_review_sample(data)
    return data
