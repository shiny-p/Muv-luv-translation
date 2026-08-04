import json
import os
import re

import cv2
import numpy as np

from .config import SEGMENTS_PATH, resolve_segments_path, video_output_dir
from .video import video_info

JP_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
MAX_IOU_GAP = 2


def _norm_box(b):
    return np.asarray(b, dtype=np.float64).reshape(-1, 2)


def _box_rect(box):
    xs = box[:, 0]
    ys = box[:, 1]
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _union_bbox(box_a, box_b):
    ax0, ay0, ax1, ay1 = _box_rect(box_a)
    bx0, by0, bx1, by1 = _box_rect(box_b)
    x0, y0 = min(ax0, bx0), min(ay0, by0)
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float64)


def _vertical_ok(a, b):
    ax0, ay0, ax1, ay1 = _box_rect(a["box"])
    bx0, by0, bx1, by1 = _box_rect(b["box"])
    inter = max(0, min(ay1, by1) - max(ay0, by0))
    smaller = min(ay1 - ay0, by1 - by0)
    if smaller <= 0:
        return False
    return inter / smaller >= 0.5


def _iou(box_a, box_b):
    ax0, ay0, ax1, ay1 = _box_rect(box_a)
    bx0, by0, bx1, by1 = _box_rect(box_b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = iw * ih
    ua = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    if ua <= 0:
        return 0.0
    return inter / ua


class RapidOCRBackend:
    def __init__(self, lang="japan"):
        try:
            from rapidocr import RapidOCR
        except ImportError:
            raise SystemExit("未安装 rapidocr，请运行: pip install rapidocr")
        try:
            self.engine = RapidOCR(params={"Rec.lang_type": lang})
        except (TypeError, ValueError):
            try:
                self.engine = RapidOCR(params={"Global.lang": lang})
            except (TypeError, ValueError):
                self.engine = RapidOCR(lang=lang)

    def recognize(self, bgr):
        res = self.engine(bgr)
        if hasattr(res, "boxes"):
            boxes = res.boxes
            txts = res.txts
            scores = res.scores
            if boxes is None:
                return []
            out = []
            for box, text, score in zip(boxes, txts, scores):
                b = _norm_box(box)
                text = str(text).strip()
                if b.size == 0 or not text:
                    continue
                out.append({"box": b, "text": text, "score": float(score)})
            return out
        if isinstance(res, tuple):
            result = res[0] if res else []
        else:
            result = res
        out = []
        for item in result or []:
            if isinstance(item, dict):
                d = item
            else:
                box, text, score = item
                d = {"box": box, "text": text, "score": score}
            box = _norm_box(d.get("box"))
            text = str(d.get("text", "")).strip()
            score = float(d.get("score", 0))
            if box.size == 0 or not text:
                continue
            out.append({"box": box, "text": text, "score": score})
        return out


def _make_backend(cfg):
    engine = cfg["ocr"]["engine"]
    if engine == "rapidocr":
        return RapidOCRBackend(cfg["ocr"].get("lang", "japan"))
    raise SystemExit("暂不支持的 OCR 引擎: %s（目前可用 rapidocr）" % engine)


def _build_kind_segments(detections, step, kind):
    merge_gap = 0 if kind == "name" else MAX_IOU_GAP * step
    segments = []
    seg_id = 0
    prev = {}
    for fidx in sorted(detections):
        matched = []
        for d in detections[fidx]:
            best = None
            for seg in prev.values():
                if fidx - seg["end"] > 2 * step:
                    continue
                ref = np.mean(np.stack(seg["boxes"]), axis=0)
                if seg["text"] == d["text"] and _iou(ref, d["box"]) > 0.3:
                    best = seg
                    break
            if best is not None:
                best["boxes"].append(d["box"])
                best["end"] = fidx
                best["score"] = max(best["score"], d["score"])
                matched.append(best)
            else:
                seg = {
                    "id": seg_id,
                    "kind": kind,
                    "text": d["text"],
                    "start": fidx,
                    "end": fidx,
                    "boxes": [d["box"]],
                    "score": d["score"],
                }
                seg_id += 1
                segments.append(seg)
                matched.append(seg)
        prev = {s["id"]: s for s in matched}

    out = []
    for seg in segments:
        if not seg["boxes"]:
            continue
        box = np.mean(np.stack(seg["boxes"]), axis=0).round().astype(int)
        out.append(
            {
                "id": seg["id"],
                "kind": kind,
                "text": seg["text"],
                "start": max(0, seg["start"] - step // 2),
                "end": seg["end"] + step // 2,
                "box": box.tolist(),
                "score": round(seg["score"], 3),
            }
        )

    merged = []
    for s in sorted(out, key=lambda x: x["start"]):
        if merged:
            last = merged[-1]
            if (
                s["text"] == last["text"]
                and s["start"] - last["end"] <= merge_gap
                and _iou(np.asarray(s["box"]), np.asarray(last["box"])) > 0.1
            ):
                last["end"] = max(last["end"], s["end"])
                continue
        merged.append(dict(s))
    return _refine_segments(merged, step, merge_text=(kind != "name"))


def _build_segments(detections, step):
    all_segs = []
    for kind in ("name", "dialogue"):
        dets = {f: [d for d in ds if d["kind"] == kind] for f, ds in detections.items()}
        dets = {f: v for f, v in dets.items() if v}
        if dets:
            all_segs.extend(_build_kind_segments(dets, step, kind))
    all_segs.sort(key=lambda s: s["start"])
    for i, s in enumerate(all_segs):
        s["id"] = i
    return all_segs


def _refine_segments(segs, step, merge_text=True):
    for s in segs:
        s["box"] = _norm_box(s["box"])
    if merge_text:
        segs = _substring_merge(segs, step)
    segs = _line_group(segs, step)
    for i, s in enumerate(sorted(segs, key=lambda x: x["start"])):
        s["id"] = i
        s["box"] = np.round(np.asarray(s["box"])).astype(int).tolist()
    return segs


def _substring_merge(segs, step):
    segs = [dict(s) for s in segs]
    changed = True
    while changed:
        changed = False
        segs.sort(key=lambda s: (s["start"], s["end"]))
        i = 0
        while i < len(segs):
            j = i + 1
            while j < len(segs):
                a, b = segs[i], segs[j]
                if a["end"] < b["start"] - step:
                    break
                if _vertical_ok(a, b) and (a["text"] in b["text"] or b["text"] in a["text"]):
                    merged = dict(b if len(b["text"]) > len(a["text"]) else a)
                    merged["start"] = min(a["start"], b["start"])
                    merged["end"] = max(a["end"], b["end"])
                    merged["box"] = _union_bbox(a["box"], b["box"])
                    segs[i] = merged
                    del segs[j]
                    changed = True
                    continue
                j += 1
            i += 1
    return segs


def _line_group(segs, step):
    segs = [dict(s) for s in segs]
    groups = []
    used = [False] * len(segs)
    for i in range(len(segs)):
        if used[i]:
            continue
        g = dict(segs[i])
        members = [segs[i]]
        used[i] = True
        for j in range(len(segs)):
            if used[j]:
                continue
            s = segs[j]
            ov = min(g["end"], s["end"]) - max(g["start"], s["start"])
            if ov < step or not _vertical_ok(g, s):
                continue
            g["start"] = min(g["start"], s["start"])
            g["end"] = max(g["end"], s["end"])
            g["box"] = _union_bbox(g["box"], s["box"])
            members.append(s)
            used[j] = True
        members.sort(key=lambda m: int(_box_rect(m["box"])[0]))
        g["text"] = "".join(m["text"] for m in members)
        groups.append(g)
    return groups


def load_segments(path=None):
    path = path or resolve_segments_path()
    if not os.path.exists(path):
        raise SystemExit("还没有 OCR 结果，请先运行: python run.py ocr <视频>")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("segments", [])


def save_segments(video, segments, fps=0.0):
    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "segments.json")
    data = {"video": video, "fps": fps, "count": len(segments), "segments": segments}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if os.path.abspath(path) != os.path.abspath(SEGMENTS_PATH) and os.path.exists(SEGMENTS_PATH):
        try:
            os.remove(SEGMENTS_PATH)
        except OSError:
            pass
    return path


def _ocr_regions(cfg, w, h):
    ocfg = cfg["ocr"]
    if ocfg.get("name_region") and ocfg.get("dialogue_region"):
        regions = []
        for kind in ("name", "dialogue"):
            r = ocfg[kind + "_region"]
            regions.append(
                (
                    kind,
                    int(r["left"] * w),
                    int(r["top"] * h),
                    int(r["right"] * w),
                    int(r["bottom"] * h),
                )
            )
        return regions
    r = ocfg.get("region", {"top": 0.62, "bottom": 1.0})
    return [("dialogue", 0, int(r["top"] * h), w, int(r["bottom"] * h))]


def run_ocr(video, cfg, force=False):
    if os.path.exists(SEGMENTS_PATH) and not force:
        print("OCR 结果已存在，跳过（加 --force 强制重跑）")
        return load_segments()

    ocfg = cfg["ocr"]
    w, h, fps, n = video_info(video)
    step = max(1, int(ocfg["sample_step"]))
    min_score = float(ocfg["min_score"])
    min_area = float(ocfg["min_area"])
    max_chars = int(ocfg["max_text_chars"])
    require_jp = bool(ocfg["require_japanese"])
    backend = _make_backend(cfg)
    regions = _ocr_regions(cfg, w, h)

    detections = {}
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % video)
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            keep = []
            for kind, rx0, ry0, rx1, ry1 in regions:
                crop = frame[ry0:ry1, rx0:rx1]
                for d in backend.recognize(crop):
                    if d["score"] < min_score:
                        continue
                    text = d["text"]
                    if not text or len(text) > max_chars:
                        continue
                    if require_jp and not JP_RE.search(text):
                        continue
                    box = d["box"]
                    r = _box_rect(box)
                    if (r[2] - r[0]) * (r[3] - r[1]) < min_area:
                        continue
                    box[:, 0] += rx0
                    box[:, 1] += ry0
                    keep.append({"box": box, "text": text, "score": d["score"], "kind": kind})
            if keep:
                detections[idx] = keep
        idx += 1
    cap.release()

    segments = _build_segments(detections, step)
    save_segments(video, segments, fps)
    print(
        "OCR 完成：识别到 %d 段（人名 %d / 台词 %d），唯一文本 %d"
        % (
            len(segments),
            len([s for s in segments if s["kind"] == "name"]),
            len([s for s in segments if s["kind"] == "dialogue"]),
            len(set(s["text"] for s in segments)),
        )
    )
    return segments
