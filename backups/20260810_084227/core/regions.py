import json
import os

import cv2
import numpy as np

from .config import video_output_dir
from .ocr import JP_RE, _make_backend
from .video import video_info

SAMPLE_COUNT = 24
BAND_GAP_FRAC = 0.035
BAND_GAP_MIN = 10
PAD_FRAC = 0.02
DIAL_MAX_X = 0.80
NAME_MAX_X_SPAN = 0.35
SCREENSHOT_COUNT = 4
REGION_FILENAME = "region.json"


def region_path(video):
    return os.path.join(video_output_dir(video), REGION_FILENAME)


def _validate_dialogue_region(region):
    if not isinstance(region, dict):
        raise ValueError("台词区必须是对象")
    try:
        left = float(region["left"])
        top = float(region["top"])
        right = float(region["right"])
        bottom = float(region["bottom"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("台词区必须含 left、top、right、bottom 四个数字") from exc
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("台词区坐标必须满足 0 ≤ left < right ≤ 1，0 ≤ top < bottom ≤ 1")
    return {
        "left": round(left, 6),
        "top": round(top, 6),
        "right": round(right, 6),
        "bottom": round(bottom, 6),
    }


def save_dialogue_region(video, dialogue_region):
    """保存该视频专属的台词区，供 OCR 与局部 OCR 复用。"""
    region = _validate_dialogue_region(dialogue_region)
    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    path = region_path(video)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"video": video, "dialogue_region": region}, f, ensure_ascii=False, indent=2)
    return region


def load_dialogue_region(video):
    path = region_path(video)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return _validate_dialogue_region(data.get("dialogue_region"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("台词区文件无效，请修正或重新检测: %s（%s）" % (path, exc))


def save_fixed_region(video, cfg, region):
    """用固定台词区生成校验截图并保存 region.json（不调用自动检测）。

    固定区域来自 config.yaml 的 region.fixed；校验截图用于人工确认，
    与自动检测一样生成 SCREENSHOT_COUNT 张。自动检测函数 detect_dialogue_region
    仍保留，仅在该函数不适用（未配置固定区域）时兜底。
    """
    region = _validate_dialogue_region(region)
    w, h, fps, n = video_info(video)
    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    box = (
        int(region["left"] * w),
        int(region["top"] * h),
        int(region["right"] * w),
        int(region["bottom"] * h),
    )
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % video)
    safe = int(fps * 10)
    frames = []
    if n > SCREENSHOT_COUNT:
        step = (n - 1) / (SCREENSHOT_COUNT - 1)
        for i in range(SCREENSHOT_COUNT):
            fidx = min(n - 1, int(i * step))
            if fidx < safe:
                fidx = safe
            cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
            ok, f = cap.read()
            if ok:
                frames.append((fidx, f.copy()))
    else:
        for i in range(n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, f = cap.read()
            if ok:
                frames.append((i, f.copy()))
    cap.release()

    saved = []
    for i, (fidx, frame) in enumerate(frames[:SCREENSHOT_COUNT]):
        img = frame.copy()
        cv2.rectangle(img, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 3)
        path = os.path.join(out_dir, "region_check_%d.png" % i)
        cv2.imwrite(path, img)
        saved.append(path)
    print("固定台词区: %s" % region)
    print("已生成 %d 张校验截图（绿色框为固定台词区，请确认后继续）:" % len(saved))
    for p in saved:
        print("  %s" % p)
    region = save_dialogue_region(video, region)
    print("已写入 %s（单视频微调可直接改该文件或使用 --region）" % region_path(video))
    return region


def _to_fraction(x0, y0, x1, y1, w, h, pad):
    x0 = max(0.0, (x0 - pad * w) / w)
    y0 = max(0.0, (y0 - pad * h) / h)
    x1 = min(1.0, (x1 + pad * w) / w)
    y1 = min(1.0, (y1 + pad * h) / h)
    return {
        "left": float(round(x0, 3)),
        "top": float(round(y0, 3)),
        "right": float(round(x1, 3)),
        "bottom": float(round(y1, 3)),
    }


def _cluster_bands(y_centers, gap):
    y_centers = sorted(y_centers)
    bands = []
    cur = [y_centers[0], y_centers[0], 1]
    for y in y_centers[1:]:
        if y - cur[1] <= gap:
            cur[1] = y
            cur[2] += 1
        else:
            bands.append(tuple(cur))
            cur = [y, y, 1]
    bands.append(tuple(cur))
    return bands


def _merge_bands(bands, gap):
    """把 y 间隔 <= gap 的相邻条带合并（同一对话框的多行台词）。"""
    bands = sorted(bands)
    merged = []
    for y0, y1, cnt in bands:
        if merged and y0 - merged[-1][1] <= gap:
            prev = merged[-1]
            merged[-1] = (prev[0], max(prev[1], y1), prev[2] + cnt)
        else:
            merged.append((y0, y1, cnt))
    return merged


def detect_dialogue_region(video, cfg, sample_count=SAMPLE_COUNT):
    w, h, fps, n = video_info(video)
    backend = _make_backend(cfg)
    ocfg = cfg["ocr"]
    min_score = float(ocfg.get("min_score", 0.5))
    require_jp = bool(ocfg.get("require_japanese", False))
    band_gap = max(BAND_GAP_MIN, int(BAND_GAP_FRAC * h))
    cap = cv2.VideoCapture(video)
    samples = []
    shots = {}
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % video)

    safe = int(fps * 10)
    sample_frames = [int(i * (n - 1) / max(1, sample_count - 1)) for i in range(sample_count)]
    candidates = [i for i, f in enumerate(sample_frames) if f >= safe]
    if len(candidates) < SCREENSHOT_COUNT:
        candidates = list(range(sample_count))
    step = max(1, len(candidates) // SCREENSHOT_COUNT)
    shot_inds = set(candidates[::step][:SCREENSHOT_COUNT])

    for i in range(sample_count):
        fidx = sample_frames[i]
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ok, f = cap.read()
        if not ok:
            continue
        if i in shot_inds:
            shots[fidx] = f.copy()
        for d in backend.recognize(f):
            if d["score"] < min_score:
                continue
            text = str(d.get("text", "")).strip()
            if not text:
                continue
            if require_jp and not JP_RE.search(text):
                continue
            b = np.asarray(d["box"]).round().astype(int)
            bx0, by0 = int(b[:, 0].min()), int(b[:, 1].min())
            bx1, by1 = int(b[:, 0].max()), int(b[:, 1].max())
            if by0 < 0.45 * h:
                continue
            if (bx1 - bx0) * (by1 - by0) < 400:
                continue
            samples.append((float(np.mean(b[:, 1])), bx0, by0, bx1, by1))
    cap.release()
    if not samples:
        raise SystemExit("未检测到文字，无法自动定位台词区")

    bands = _cluster_bands([s[0] for s in samples], band_gap)
    maxc = max(b[2] for b in bands)
    th = max(3, 0.25 * maxc)
    persistent = [b for b in bands if b[2] >= th]

    def is_timer(band):
        mids = [s for s in samples if band[0] <= s[0] <= band[1]]
        if not mids:
            return False
        cx = [((s[1] + s[3]) / 2) / w for s in mids]
        cy = [s[0] / h for s in mids]
        return all(x > 0.8 for x in cx) and all(y > 0.85 for y in cy)

    persistent = [b for b in persistent if not is_timer(b)]
    if not persistent:
        raise SystemExit("检测到的文字不够稳定，无法自动定位台词区")

    def band_boxes(band):
        return [s for s in samples if band[0] - band_gap <= s[0] <= band[1] + band_gap]

    def band_is_name_like(band):
        boxes = band_boxes(band)
        if not boxes:
            return False
        spans = [s[3] - s[1] for s in boxes]
        return max(spans) < NAME_MAX_X_SPAN * w

    if len(persistent) >= 2 and band_is_name_like(persistent[0]):
        print("检测到顶部短标签带（说话人名），已剔除，只保留台词区")
        persistent = persistent[1:]
        if not persistent:
            raise SystemExit("剔除人名带后无剩余文字，无法定位台词区")

    # 同一对话框的多行台词若被拆成多条带，先合并再求范围
    persistent = _merge_bands(persistent, band_gap)

    gb = []
    for band in persistent:
        for s in band_boxes(band):
            if ((s[1] + s[3]) / 2) / w <= DIAL_MAX_X:
                gb.append([s[1], s[2], s[3], s[4]])
    if not gb:
        raise SystemExit("台词区检测失败，请手工编辑 region.json 或使用 --region 指定区域")
    arr = np.array(gb, dtype=np.float64)
    dialogue_region = _to_fraction(arr[:, 0].min(), arr[:, 1].min(),
                                   arr[:, 2].max(), arr[:, 3].max(), w, h, PAD_FRAC)

    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    box = (
        int(dialogue_region["left"] * w),
        int(dialogue_region["top"] * h),
        int(dialogue_region["right"] * w),
        int(dialogue_region["bottom"] * h),
    )
    saved = []
    for i, (fidx, frame) in enumerate(sorted(shots.items())):
        img = frame.copy()
        cv2.rectangle(img, (box[0], box[1]), (box[2], box[3]), (0, 255, 0), 3)
        path = os.path.join(out_dir, "region_check_%d.png" % i)
        cv2.imwrite(path, img)
        saved.append(path)

    print("自动检测台词区: %s" % dialogue_region)
    print("已生成 %d 张校验截图（绿色框为检测到的台词区，请确认后继续）:" % len(saved))
    for p in saved:
        print("  %s" % p)
    dialogue_region = save_dialogue_region(video, dialogue_region)
    print("已写入 %s（若不准可手动修改后重跑 ocr）" % region_path(video))
    return dialogue_region
