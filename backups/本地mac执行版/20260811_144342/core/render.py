import json
import math
import os
import re
import shutil
import subprocess
import sys

import cv2
import numpy as np

try:
    import fcntl
except ImportError:
    fcntl = None
from PIL import Image, ImageDraw, ImageFont
from tqdm import tqdm

from .config import (
    SEGMENTS_PATH,
    TRANSLATIONS_PATH,
    resolve_segments_path,
    resolve_translations_path,
    video_output_dir,
)
from .video import video_info, write_cmd, decode_frames_cmd

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


class _PipeReader:
    """ffmpeg NVDEC/CUDA 硬解读取器：stdout 输出 BGR 帧（可按 out_w/out_h 预先缩放）。"""

    def __init__(self, video, out_w, out_h, cfg):
        self.w, self.h = out_w, out_h
        self.scaled = True
        self.proc = subprocess.Popen(
            decode_frames_cmd(video, cfg, out_w=out_w, out_h=out_h),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        _enlarge_pipe(self.proc.stdout)

    def read(self):
        need = self.w * self.h * 3
        data = self.proc.stdout.read(need)
        if len(data) < need:
            return False, None
        return True, np.frombuffer(data, np.uint8).reshape(self.h, self.w, 3)

    def release(self):
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            pass


class _CvReader:
    """OpenCV 软解读取器（无 hwaccel 时使用）。"""

    def __init__(self, video):
        self.cap = cv2.VideoCapture(video)
        self.scaled = False

    def read(self):
        return self.cap.read()

    def release(self):
        self.cap.release()


def _open_reader(video, out_w, out_h, cfg):
    """按配置选择解码器：hwaccel=cuda 用 ffmpeg 硬解（可预缩放），否则 OpenCV 软解。"""
    if decode_frames_cmd(video, cfg, out_w=out_w, out_h=out_h) is not None:
        return _PipeReader(video, out_w, out_h, cfg)
    return _CvReader(video)


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


_FONT_CACHE = {}
_LAYOUT_CACHE = {}
_TEXT_STRIP_CACHE = {}


def _enlarge_pipe(stream, size=8 * 1024 * 1024):
    """放大管道缓冲区：默认 64KB 管道会让大帧读写反复阻塞，是渲染吞吐的瓶颈。"""
    if fcntl is None:
        return
    try:
        fd = stream.fileno()
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, size)
    except (OSError, ValueError, AttributeError):
        pass




def _get_font(path, size):
    """带缓存的字体加载：避免每帧重复解析 CJK 字体文件（曾是最主要的渲染开销）。"""
    key = (path, size)
    f = _FONT_CACHE.get(key)
    if f is None:
        f = ImageFont.truetype(path, size)
        if len(_FONT_CACHE) > 64:
            _FONT_CACHE.clear()
        _FONT_CACHE[key] = f
    return f


def _layout_text(dtext, font_path, max_w, sw, font_size):
    """带缓存的文本排版：同一句台词在多帧间只排一次版。返回 (字号, 行列表)。"""
    key = (dtext, max_w, font_size)
    hit = _LAYOUT_CACHE.get(key)
    if hit is not None:
        return hit
    size = font_size
    f = _get_font(font_path, size)
    lines = _wrap_text(dtext, f, max_w - 2 * sw)
    while len(lines) > 3 and size > 14:
        size -= 2
        f = _get_font(font_path, size)
        lines = _wrap_text(dtext, f, max_w - 2 * sw)
    if len(_LAYOUT_CACHE) > 512:
        _LAYOUT_CACHE.clear()
    _LAYOUT_CACHE[key] = (size, lines)
    return size, lines


def _render_frame_append(frame, idx, segments, translations, cfg, font_path, font_size,
                         out_w, h_src, append_h):
    active = [s for s in segments if s["start"] <= idx <= s["end"]]
    dlg = [s for s in active if _translation(translations, s)]
    kept = []
    for s in dlg:
        sb = np.asarray(s["box"], np.float64)
        ns = re.sub(r"\s+", "", s["text"])
        drop = False
        for t in dlg:
            if t is s or len(t["text"]) <= len(s["text"]):
                continue
            tb = np.asarray(t["box"], np.float64)
            if (sb[:, 1].min() >= tb[:, 1].min() - 12 and sb[:, 1].max() <= tb[:, 1].max() + 12
                    and ns and ns in re.sub(r"\s+", "", t["text"])):
                drop = True
                break
        if not drop:
            kept.append(s)
    dlg = sorted(kept, key=_box_top)

    seen = set()
    parts = []
    for s in dlg:
        t = _translation(translations, s)
        if t and t not in seen:
            seen.add(t)
            parts.append(t)
    dtext = "\n".join(parts)

    base_color = _resolve_strip_color(frame, cfg, h_src, out_w)
    color_key = tuple((base_color // 16).tolist())
    cache_key = (dtext, font_size, color_key)
    cached = _TEXT_STRIP_CACHE.get(cache_key)
    if cached is not None:
        return np.vstack([frame, cached])
    strip = np.zeros((append_h, out_w, 3), np.uint8)
    strip[:] = base_color
    if dtext:
        rcfg = cfg["render"]
        sw = int(rcfg["stroke"])
        color = rcfg["font_color"]
        sc = rcfg["stroke_color"]
        margin = float(rcfg.get("frame_margin", 0.03))
        max_w = int(out_w * (1 - 2 * margin))
        pad = max(10, int(append_h * 0.06))
        size, lines = _layout_text(dtext, font_path, max_w, sw, font_size)
        f = _get_font(font_path, size)
        lh = _line_height(f)
        gap = size // 3
        total = lh * len(lines) + gap * (len(lines) - 1)
        top = pad
        bottom = append_h - pad
        cy = top + max(0, (bottom - top) - total) / 2 + lh / 2
        # 只在条带区域绘制文字，避免整帧 PIL 转换
        img = Image.fromarray(cv2.cvtColor(strip, cv2.COLOR_BGR2RGB))
        d = ImageDraw.Draw(img)
        for line in lines:
            d.text((out_w / 2, cy), line, font=f, fill=color, anchor="mm",
                   stroke_width=sw, stroke_fill=sc)
            cy += lh + gap
        strip[:, :] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        if len(_TEXT_STRIP_CACHE) > 512:
            _TEXT_STRIP_CACHE.clear()
        _TEXT_STRIP_CACHE[cache_key] = strip.copy()
    return np.vstack([frame, strip])


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

    stem = os.path.splitext(os.path.basename(video))[0]
    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(cfg.get("output") or "output.mp4"))
    n_out = int(round(n * target_fps / fps))
    if test_frames > 0:
        n_out = min(n_out, test_frames)

    cmd = write_cmd(video, out_w, out_h, target_fps, out, int(rcfg["crf"]), rcfg["preset"], cfg)
    print("渲染输出: %s (%dx%d @%.0ffps) 共 %d 帧" % (out, out_w, out_h, target_fps, n_out))
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    _enlarge_pipe(proc.stdin)

    reader = _open_reader(video, out_w, int(round(h * sf)), cfg)
    next_out = 0
    src_i = 0
    pbar = tqdm(total=n_out, desc="渲染中")
    try:
        while True:
            if next_out >= n_out:
                break
            ok, frame = reader.read()
            if not ok:
                break
            if sf != 1.0 and not getattr(reader, "scaled", False):
                frame = cv2.resize(frame, (out_w, int(round(h * sf))), interpolation=cv2.INTER_AREA)
            frame = _render_frame_append(
                frame, src_i, segs, translations, cfg, font_path, font_size,
                out_w, int(round(h * sf)), append_h,
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
        reader.release()
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
    # 原始视频一并放入输出文件夹（若尚未在其中；CFR 视频已在输出文件夹内则跳过）
    backup_video = os.path.join(out_dir, os.path.basename(video))
    if os.path.abspath(video) != os.path.abspath(backup_video):
        try:
            shutil.move(video, backup_video)
            print("原视频已移动到: %s" % backup_video)
        except Exception as e:
            print("移动原视频失败: %s" % e)
    print("完成，输出文件: %s" % out)
