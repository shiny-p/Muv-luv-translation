import json
import os
import re
import time

from .config import (
    GLOSSARY_PATH,
    TRANSLATIONS_PATH,
    resolve_segments_path,
    resolve_translations_path,
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

SYSTEM_PROMPT_NAME = (
    "你是字幕翻译。把日语角色名翻译成简体中文。"
    "要求：只输出译名这一个词，不要任何解释、引号或标点；"
    "译名要简洁自然，符合中文称呼习惯，同一角色全程保持一致。"
)

GLOSSARY_TEMPLATE = """\
{
  "names": {},
  "proper_nouns": {},
  "_说明": "names=人名，proper_nouns=专有名词。把日语原文对应的中文译名填到引号内，例如 \\"威厳のある女性\\": \\"威严的女性\\"。留空表示先用模型翻译。下划线开头的键会被忽略。填好后再跑 translate 即生效。"
}
"""


def load_translations(path=None):
    path = path or resolve_translations_path()
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
    names = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    dialogues = raw.get("dialogues") if isinstance(raw.get("dialogues"), dict) else {}
    names = {str(k): (str(v["translation"]) if isinstance(v, dict) else str(v)) for k, v in names.items() if v}
    dialogues = {str(k): (str(v["translation"]) if isinstance(v, dict) else str(v)) for k, v in dialogues.items() if v}
    for k, v in raw.items():
        if k in ("names", "dialogues", "timestamps") or str(k).startswith("_"):
            continue
        t = v["translation"] if isinstance(v, dict) else v
        t = str(t).strip()
        if t and str(k) not in names and str(k) not in dialogues:
            dialogues[str(k)] = t
    return {"names": names, "dialogues": dialogues}


def save_translations(data, path, name_seen=None, dial_seen=None):
    name_seen = name_seen or {}
    dial_seen = dial_seen or {}
    out = {
        "names": data["names"],
        "dialogues": data["dialogues"],
        "timestamps": {
            "names": {k: [round(x, 2) for x in v] for k, v in name_seen.items()},
            "dialogues": {k: [round(x, 2) for x in v] for k, v in dial_seen.items()},
        },
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


def ensure_glossary(name_texts):
    data = load_glossary()
    changed = False
    for text in sorted(name_texts):
        if text not in data["names"] and text not in data["proper_nouns"]:
            data["names"][text] = ""
            changed = True
    if changed:
        with open(GLOSSARY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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

    client = OpenAI(base_url=base_url, api_key=resolve_api_key(cfg))
    return client, model, False


def _translate_one(client, model, text, system_prompt):
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content.strip()


def _safe_translate(client, model, text, system_prompt):
    last_err = None
    for attempt in range(4):
        try:
            return _translate_one(client, model, text, system_prompt)
        except Exception as e:
            last_err = e
            time.sleep(2 + 2 * attempt)
    print("翻译失败: %r -> %s" % (text, last_err))
    return None


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

    name_texts = sorted({s["text"] for s in segments if s.get("kind") == "name"})
    dialogue_texts = sorted({s["text"] for s in segments if s.get("kind") != "name"})

    def seen(texts, kind):
        info = {}
        for s in segments:
            if s.get("kind") != kind or s["text"] not in texts:
                continue
            t0, t1 = s["start"], s["end"]
            cur = info.get(s["text"])
            if cur is None:
                info[s["text"]] = [t0, t1]
            else:
                info[s["text"]][0] = min(cur[0], t0)
                info[s["text"]][1] = max(cur[1], t1)
        out = {}
        for k, (a, b) in info.items():
            out[k] = [a / fps if fps else a, b / fps if fps else b]
        return out

    name_seen = seen(name_texts, "name")
    dial_seen = seen(dialogue_texts, "dialogue")

    tcfg = cfg["translation"]
    client, model, is_mock = _make_client(tcfg)
    data = load_translations(translations_path)
    glossary = ensure_glossary(name_texts)
    terms = glossary_terms(glossary)
    changed = False

    for text in name_texts:
        cur = data["names"].get(text, "")
        if terms.get(text):
            data["names"][text] = terms[text]
            changed = True
        elif not cur:
            if is_mock:
                data["names"][text] = "[译] %s" % text
            else:
                zh = _safe_translate(client, model, text, SYSTEM_PROMPT_NAME)
                if zh:
                    data["names"][text] = zh
            changed = True

    for text in dialogue_texts:
        cur = data["dialogues"].get(text, "")
        if not cur:
            if is_mock:
                data["dialogues"][text] = "[译] %s" % text
            elif terms.get(text):
                data["dialogues"][text] = terms[text]
            else:
                source, mapping = _substitute_glossary(text, terms)
                zh = _safe_translate(client, model, source, SYSTEM_PROMPT_DIALOGUE)
                if zh:
                    data["dialogues"][text] = _restore_glossary(zh, mapping)
            changed = True

    name_set = set(name_texts)
    dial_set = set(dialogue_texts)
    data["names"] = {k: v for k, v in data["names"].items() if k in name_set}
    data["dialogues"] = {k: v for k, v in data["dialogues"].items() if k in dial_set}

    if changed:
        save_translations(data, translations_path, name_seen, dial_seen)

    if os.path.abspath(translations_path) != os.path.abspath(TRANSLATIONS_PATH) and os.path.exists(TRANSLATIONS_PATH):
        try:
            os.remove(TRANSLATIONS_PATH)
        except OSError:
            pass

    unfilled_names = [t for t in name_texts if not terms.get(t)]
    missing = [t for t in name_texts if not data["names"].get(t)]
    missing += [t for t in dialogue_texts if not data["dialogues"].get(t)]
    print("翻译完成：人名 %d 条，台词 %d 条 → %s" % (len(data["names"]), len(data["dialogues"]), translations_path))
    if unfilled_names:
        print("以下人名未在 glossary.json 中填写，已用模型临时翻译，请核对后填入词典：")
        for t in unfilled_names:
            print("  %s -> %s" % (t, data["names"].get(t, "")))
    if missing:
        print("以下 %d 条翻译失败，可稍后重跑 translate 补齐：%s" % (len(missing), missing))
    return data
