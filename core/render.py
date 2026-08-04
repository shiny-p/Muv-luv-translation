import json
import math
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm

from .config import (
    SEGMENTS_PATH,
    TRANSLATIONS_PATH,
    resolve_segments_path,
    resolve_translations_path,
    video_output_dir,
)
from .video import video_info, write_cmd

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


def _load_segments(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("segments", [])


def _load_translations(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f) or {}


def find_font(name):
    if name and os.path.exists(name):
        return name
    for c in FONT_CANDIDATES:
        if os.path.exists(c):
            return c
    raise SystemExit("未找到中文字体，请在 config.yaml 的 render.font 指定路径")


def _line_height(font):
    ascent, descent = font.getmetrics()
    return ascent + descent


_PUNCT = "，。！？、；：…．．，．"


def _wrap_text(text, font, max_w):
    out = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        if font.getlength(para) <= max_w:
            out.append(para)
            continue
        out.extend(_split_paragraph(para, font, max_w))
    return out or [text]


def _split_paragraph(para, font, max_w):
    total = font.getlength(para)
    n = max(2, math.ceil(total / max_w))
    if n <= 2:
        best = None
        best_score = None
        ln = len(para)
        for i in range(1, ln):
            left, right = para[:i], para[i:]
            if font.getlength(left) > max_w or font.getlength(right) > max_w:
                continue
            score = abs(i - ln / 2)
            if para[i - 1] in _PUNCT:
                score -= 2
            if best_score is None or score < best_score:
                best_score = score
                best = i
        if best is not None:
            return [para[:best], para[best:]]

    lines = []
    cur = ""
    for ch in para:
        cand = cur + ch
        if font.getlength(cand) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)

    while len(lines) >= 2:
        prev, last = lines[-2], lines[-1]
        if len(prev) <= 1 or font.getlength(last) >= max_w:
            break
        if len(prev) - len(last) > 1:
            ch = prev[-1]
            if font.getlength(ch + last) <= max_w:
                lines[-2] = prev[:-1]
                lines[-1] = ch + last
                continue
        break
    return lines


def _translation(translations, seg):
    kind = seg.get("kind", "dialogue")
    section = translations.get(kind + "s")
    if not isinstance(section, dict):
        section = translations
    ent = section.get(seg["text"])
    if isinstance(ent, dict):
        return ent.get("translation")
    return ent


def _box_height(box):
    xs = box[:, 0]
    ys = box[:, 1]
    return max(1, int(ys.max() - ys.min()))


def _global_font_size(segments, rcfg, sf):
    heights = [int(_box_height(np.asarray(s["box"]) * sf)) for s in segments]
    heights.sort()
    n = len(heights)
    if n == 0:
        return 34
    if n % 2 == 1:
        med = heights[n // 2]
    else:
        med = (heights[n // 2 - 1] + heights[n // 2]) / 2
    size = int(med * rcfg.get("font_scale", 0.78))
    return max(10, size)


def _scale_segments(segments, sf):
    out = []
    for s in segments:
        box = np.asarray(s["box"], np.float64) * sf
        out.append({**s, "box": box.round().astype(int)})
    return out


def _box_top(s):
    b = np.asarray(s["box"])
    return float(b[:, 1].min())


def _strip_color_from_frame(frame, h, w):
    y0 = int(h * 0.90)
    y1 = max(y0 + 1, int(h * 0.98))
    x0 = int(w * 0.20)
    x1 = int(w * 0.75)
    sub = frame[y0:y1, x0:x1]
    if sub.size == 0:
        return np.array([104, 120, 124], np.uint8)
    return sub.reshape(-1, 3).mean(axis=0).astype(np.uint8)


def _resolve_strip_color(frame, cfg, h, w):
    bg = cfg["render"].get("append_bg", "auto")
    if isinstance(bg, str):
        bg = bg.strip().lower()
        if bg == "auto":
            return _strip_color_from_frame(frame, h, w)
        if bg == "black":
            return np.zeros(3, np.uint8)
        if bg.startswith("#"):
            hexs = bg[1:].strip()
            r = int(hexs[0:2], 16)
            g = int(hexs[2:4], 16)
            b = int(hexs[4:6], 16)
            return np.array([b, g, r], np.uint8)
    return _strip_color_from_frame(frame, h, w)


def _render_frame_append(frame, idx, segments, translations, cfg, font_path, font_size,
                         out_w, h_src, append_h, name_x_frac):
    active = [s for s in segments if s["start"] <= idx <= s["end"]]
    names = [s for s in active if s.get("kind") == "name"]
    dlg = [s for s in active if s.get("kind") != "name"]
    dlg = sorted([s for s in dlg if _translation(translations, s)], key=_box_top)
    ntext = _translation(translations, names[0]) if names else None

    seen = set()
    parts = []
    for s in dlg:
        t = _translation(translations, s)
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    dtext = "\n".join(parts)

    strip = np.zeros((append_h, out_w, 3), np.uint8)
    strip[:] = _resolve_strip_color(frame, cfg, h_src, out_w)
    out = np.vstack([frame, strip])
    if not ntext and not dtext:
        return out

    rcfg = cfg["render"]
    sw = int(rcfg["stroke"])
    color = rcfg["font_color"]
    sc = rcfg["stroke_color"]
    img = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    margin = float(rcfg.get("frame_margin", 0.03))
    max_w = int(out_w * (1 - 2 * margin))
    pad = max(10, int(append_h * 0.06))

    top = h_src + pad
    if ntext:
        nf = ImageFont.truetype(font_path, max(12, font_size - 4))
        d.text((int(name_x_frac * out_w), top + _line_height(nf) / 2), ntext,
               font=nf, fill=color, anchor="lm", stroke_width=sw, stroke_fill=sc)
        top += _line_height(nf) + 12

    if dtext:
        size = font_size
        f = ImageFont.truetype(font_path, size)
        lines = _wrap_text(dtext, f, max_w - 2 * sw)
        while len(lines) > 3 and size > 14:
            size -= 2
            f = ImageFont.truetype(font_path, size)
            lines = _wrap_text(dtext, f, max_w - 2 * sw)
        lh = _line_height(f)
        gap = size // 3
        total = lh * len(lines) + gap * (len(lines) - 1)
        bottom = h_src + append_h - pad
        cy = top + max(0, (bottom - top) - total) / 2 + lh / 2
        for line in lines:
            d.text((out_w / 2, cy), line, font=f, fill=color, anchor="mm",
                   stroke_width=sw, stroke_fill=sc)
            cy += lh + gap

    out[:, :] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    return out


def _resolve_video(video):
    if video and os.path.exists(video):
        return video
    if not video:
        seg = resolve_segments_path()
        if os.path.exists(seg):
            try:
                with open(seg, encoding="utf-8") as f:
                    video = json.load(f).get("video") or ""
            except Exception:
                pass
    if video and os.path.exists(video):
        return video
    if video:
        stem = os.path.splitext(os.path.basename(video))[0]
        base = os.path.dirname(video) or "."
        cand = os.path.join(base, stem + "_output", os.path.basename(video))
        if os.path.exists(cand):
            return cand
    return video or ""


def run_render(cfg, force=False, video=None):
    video = _resolve_video(video)
    if not video:
        raise SystemExit("请指定输入视频（命令行参数，或先运行 ocr）")
    if not os.path.exists(video):
        raise SystemExit("视频文件不存在: %s" % video)

    segments_path = resolve_segments_path(video)
    translations_path = resolve_translations_path(video)
    if not os.path.exists(segments_path):
        raise SystemExit("还没有 OCR 结果，请先运行: python run.py ocr <视频>")
    if not os.path.exists(translations_path):
        raise SystemExit("还没有翻译结果，请先运行: python run.py translate")

    segments = _load_segments(segments_path)
    translations = _load_translations(translations_path)
    usable = [s for s in segments if _translation(translations, s)]
    missing = [s for s in segments if not _translation(translations, s)]
    if not usable:
        raise SystemExit("没有任何已翻译的字幕，无法渲染")
    if missing:
        print("警告：%d 段字幕未翻译，将保留原文" % len(missing))

    w, h, fps, n = video_info(video)
    rcfg = cfg["render"]
    out_w = int(rcfg["width"]) or w
    sf = out_w / w
    target_fps = float(rcfg["fps"]) or float(fps)
    test_frames = int(rcfg["test_frames"])

    font_path = find_font(rcfg["font"])
    segs = _scale_segments(usable, sf)
    segs.sort(key=lambda x: x["start"])
    font_size = _global_font_size(segs, rcfg, 1.0)
    print("全局字号: %d px（字幕框高度中位数 × %.2f）" % (font_size, rcfg.get("font_scale", 0.78)))

    append_h = int(round(int(rcfg.get("append_height", 160)) * sf))
    out_h = int(round(h * sf)) + append_h
    name_x_frac = (cfg["ocr"].get("name_region") or {}).get("left", 0.0) or 0.0

    stem = os.path.splitext(os.path.basename(video))[0]
    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(cfg.get("output") or "output.mp4"))
    n_out = int(round(n * target_fps / fps))
    if test_frames > 0:
        n_out = min(n_out, test_frames)

    cmd = write_cmd(video, out_w, out_h, target_fps, out, int(rcfg["crf"]), rcfg["preset"])
    print("渲染输出: %s (%dx%d @%.0ffps) 共 %d 帧" % (out, out_w, out_h, target_fps, n_out))
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )

    cap = cv2.VideoCapture(video)
    next_out = 0
    src_i = 0
    pbar = tqdm(total=n_out, desc="渲染中")
    try:
        while True:
            if next_out >= n_out:
                break
            ok, frame = cap.read()
            if not ok:
                break
            if sf != 1.0:
                frame = cv2.resize(frame, (out_w, int(round(h * sf))), interpolation=cv2.INTER_AREA)
            frame = _render_frame_append(
                frame, src_i, segs, translations, cfg, font_path, font_size,
                out_w, int(round(h * sf)), append_h, name_x_frac,
            )
            while next_out < n_out and int(next_out * fps / target_fps) <= src_i:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                proc.stdin.write(rgb.tobytes())
                next_out += 1
                pbar.update(1)
            src_i += 1
        while next_out < n_out:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            proc.stdin.write(rgb.tobytes())
            next_out += 1
            pbar.update(1)
    except BrokenPipeError:
        proc.stdin.close()
        proc.wait()
        err = proc.stderr.read().decode("utf-8", errors="replace")
        pbar.close()
        raise SystemExit("ffmpeg 编码失败：%s" % err)
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    pbar.close()

    rc = proc.wait()
    if rc != 0:
        err = proc.stderr.read().decode("utf-8", errors="replace")
        raise SystemExit("ffmpeg 编码失败：%s" % err)

    for src, fname in ((segments_path, "segments.json"), (translations_path, "translations.json")):
        dst = os.path.join(out_dir, fname)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
            if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(out_dir)):
                try:
                    os.remove(src)
                except OSError:
                    pass
    for stale in (SEGMENTS_PATH, TRANSLATIONS_PATH):
        if os.path.exists(stale) and os.path.dirname(os.path.abspath(stale)) != os.path.abspath(out_dir):
            try:
                os.remove(stale)
            except OSError:
                pass
    ocr_dir = os.path.dirname(SEGMENTS_PATH)
    if os.path.isdir(ocr_dir) and not os.listdir(ocr_dir):
        try:
            os.rmdir(ocr_dir)
        except OSError:
            pass
    moved_video = os.path.join(out_dir, os.path.basename(video))
    if os.path.abspath(video) != os.path.abspath(moved_video):
        try:
            shutil.move(video, moved_video)
            print("原视频已移动到: %s" % moved_video)
        except Exception as e:
            print("移动原视频失败: %s" % e)
    print("完成，输出文件: %s" % out)
