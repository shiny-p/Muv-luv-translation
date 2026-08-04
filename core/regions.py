import os

import cv2
import numpy as np

from .config import video_output_dir
from .ocr import _make_backend
from .video import video_info

SAMPLE_COUNT = 24
BAND_GAP = 35
PAD_FRAC = 0.02
DIAL_MAX_X = 0.80
NAME_MAX_X_SPAN = 0.35
SCREENSHOT_COUNT = 4


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


def detect_dialogue_region(video, cfg, sample_count=SAMPLE_COUNT):
    w, h, fps, n = video_info(video)
    backend = _make_backend(cfg)
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

    bands = _cluster_bands([s[0] for s in samples], BAND_GAP)
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
        return [s for s in samples if band[0] - BAND_GAP <= s[0] <= band[1] + BAND_GAP]

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

    gb = []
    for band in persistent:
        for s in band_boxes(band):
            if ((s[1] + s[3]) / 2) / w <= DIAL_MAX_X:
                gb.append([s[1], s[2], s[3], s[4]])
    if not gb:
        raise SystemExit("台词区检测失败，请手动填写 dialogue_region")
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
    print("已写入 config.yaml（若不准可手动修改后重跑 ocr）")
    return dialogue_region
