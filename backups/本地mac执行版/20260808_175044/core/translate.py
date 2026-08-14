import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

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


def _substitute_glossary(text, terms):
    keys = sorted(terms, key=len, reverse=True)
    mapping = {}
    new_text = text
    for i, key in enumerate(keys):
        if key in new_text:
            token = "⟨⟨%d⟩⟩" % i
            new_text = new_text.replace(key, token)
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

    # --force 会重译所有非词典锁定的台词；词典词始终以既定译名为准。
    todo = [t for t in dialogue_texts if force or not data.get(t)]
    if todo:
        for t in list(todo):
            if terms.get(t):
                data[t] = terms[t]
        todo = [t for t in todo if not terms.get(t)]

    if todo:
        print("开始翻译 %d 条台词（%s / %s）..." % (len(todo), tcfg.get("provider"), model))
        if is_mock:
            for t in todo:
                data[t] = "[译] %s" % t
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
    _print_translation_review_sample(data)
    return data
