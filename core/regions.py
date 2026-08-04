import cv2
import numpy as np

from .ocr import _make_backend
from .video import video_info

SAMPLE_COUNT = 24
BAND_GAP = 35
PERSIST_RATIO = 0.30
PAD_FRAC = 0.02
NAME_MAX_X = 0.75
DIAL_MAX_X = 0.80


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


def detect_regions(video, cfg, sample_count=SAMPLE_COUNT):
    w, h, fps, n = video_info(video)
    backend = _make_backend(cfg)
    cap = cv2.VideoCapture(video)
    samples = []
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % video)
    for i in range(sample_count):
        fidx = int(i * (n - 1) / max(1, sample_count - 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ok, f = cap.read()
        if not ok:
            continue
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
        raise SystemExit("未检测到文字，无法自动定位区域")

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
        raise SystemExit("检测到的文字不够稳定，无法自动定位区域")

    dialogue_band = max(persistent, key=lambda b: b[2])
    above = [b for b in persistent if b[1] < dialogue_band[0]]
    name_band = max(above, key=lambda b: b[1]) if above else None

    def group_boxes(band, max_x_frac):
        out = []
        for s in samples:
            if band[0] - BAND_GAP <= s[0] <= band[1] + BAND_GAP:
                if ((s[1] + s[3]) / 2) / w <= max_x_frac:
                    out.append([s[1], s[2], s[3], s[4]])
        return out

    name_region = None
    if name_band is not None:
        gb = group_boxes(name_band, NAME_MAX_X)
        if gb:
            arr = np.array(gb, dtype=np.float64)
            name_region = _to_fraction(arr[:, 0].min(), arr[:, 1].min(),
                                       arr[:, 2].max(), arr[:, 3].max(), w, h, PAD_FRAC)

    gb = group_boxes(dialogue_band, DIAL_MAX_X)
    if not gb:
        raise SystemExit("台词区检测失败，请手动填写 dialogue_region")
    arr = np.array(gb, dtype=np.float64)
    dialogue_region = _to_fraction(arr[:, 0].min(), arr[:, 1].min(),
                                   arr[:, 2].max(), arr[:, 3].max(), w, h, PAD_FRAC)

    if name_region and dialogue_region:
        dialogue_region["top"] = max(dialogue_region["top"], name_region["bottom"])

    print("自动检测结果：")
    if name_region:
        print("  人名区: %s" % name_region)
    print("  台词区: %s" % dialogue_region)
    print("已写入 config.yaml（如不准可手动修改后重跑 ocr）")
    return name_region, dialogue_region
