import json
import os
import re

import cv2
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed

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
    def __init__(self, lang="japan", intra_threads=None, use_gpu=False):
        try:
            from rapidocr import RapidOCR
        except ImportError:
            raise SystemExit("未安装 rapidocr，请运行: pip install rapidocr")
        params = {"Rec.lang_type": lang}
        if use_gpu:
            try:
                import onnxruntime as ort
            except ImportError:
                raise SystemExit("未安装 onnxruntime，请运行: pip install onnxruntime-gpu")
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise SystemExit(
                    "onnxruntime 未启用 CUDA：请安装 onnxruntime-gpu，"
                    "并确认 NVIDIA 驱动/CUDA 环境可用（config.yaml 的 ocr.use_gpu=true）"
                )
            params["EngineConfig.onnxruntime.use_cuda"] = True
        elif intra_threads and intra_threads > 0:
            # 并行 OCR 时每个子进程只占 1 个推理线程，避免多进程互相争抢 CPU
            params["EngineConfig.onnxruntime.intra_op_num_threads"] = int(intra_threads)
        try:
            self.engine = RapidOCR(params=params)
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


def _make_backend(cfg, intra_threads=None):
    engine = cfg["ocr"]["engine"]
    if engine == "rapidocr":
        return RapidOCRBackend(
            cfg["ocr"].get("lang", "japan"),
            intra_threads=intra_threads,
            use_gpu=bool(cfg["ocr"].get("use_gpu", False)),
        )
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
    return _build_kind_segments(detections, step, "dialogue")


def _refine_segments(segs, step, merge_text=True):
    for s in segs:
        s["box"] = _norm_box(s["box"])
    if merge_text:
        segs = _substring_merge(segs, step)
        segs = _typewriter_merge(segs, step)
    segs = _line_group(segs, step)
    for i, s in enumerate(sorted(segs, key=lambda x: x["start"])):
        s["id"] = i
        s["box"] = np.round(np.asarray(s["box"])).astype(int).tolist()
    return segs


def _norm_text(t):
    return re.sub(r"\s+", "", t or "")


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
                na, nb = _norm_text(a["text"]), _norm_text(b["text"])
                if _vertical_ok(a, b) and (na in nb or nb in na):
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


def _edit_dist(a, b):
    """简单编辑距离（用于短文本前缀容错）。"""
    m, n = len(a), len(b)
    if m > n:
        a, b = b, a
        m, n = n, m
    if n - m > 8:
        return 9
    dp = list(range(n + 1))
    for i, ca in enumerate(a, 1):
        prev = dp[0]
        dp[0] = i
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (ca != cb))
            prev = cur
    return dp[-1]


def _fuzzy_prefix_ok(short, long, tol):
    """short 是否为 long 的开头（允许 tol 个字符的 OCR 误差）。"""
    if not short or len(short) < 2 or len(long) < len(short):
        return False
    return _edit_dist(short, long[: len(short)]) <= tol


def _typewriter_merge(segs, step):
    """合并逐字显示(typewriter)产生的片段：
    同一位置、时间相邻、且短文本≈长文本开头（允许打字瞬间 OCR 错字）的段，
    合并为一条完整文本，时间取首尾并集。"""
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
                na, nb = _norm_text(a["text"]), _norm_text(b["text"])
                if len(na) <= len(nb):
                    short, long, sa, sb = na, nb, a, b
                else:
                    short, long, sa, sb = nb, na, b, a
                tol = max(1, int(0.15 * len(short)))
                if (
                    len(short) >= 2
                    and _vertical_ok(sa, sb)
                    and _fuzzy_prefix_ok(short, long, tol)
                ):
                    merged = dict(sb)  # 保留较长(更完整)的文本
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
            gx0, gx1 = float(g["box"][:, 0].min()), float(g["box"][:, 0].max())
            sx0, sx1 = float(s["box"][:, 0].min()), float(s["box"][:, 0].max())
            xov = min(gx1, sx1) - max(gx0, sx0)
            if xov > 0.15 * min(gx1 - gx0, sx1 - sx0):
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


def _merge_near_duplicate_segments(segs, max_gap):
    """合并局部 OCR 边界处同一位置、同一文本的重复段。"""
    out = []
    for source in sorted(segs, key=lambda s: (s["start"], s["end"])):
        s = dict(source)
        s["box"] = _norm_box(s["box"])
        match = None
        for candidate in reversed(out):
            if s["start"] - candidate["end"] > max_gap:
                break
            if s["text"] == candidate["text"] and _iou(s["box"], candidate["box"]) > 0.3:
                match = candidate
                break
        if match is None:
            out.append(s)
            continue
        match["start"] = min(match["start"], s["start"])
        match["end"] = max(match["end"], s["end"])
        match["box"] = _union_bbox(match["box"], s["box"])
        match["score"] = max(match.get("score", 0), s.get("score", 0))
    for s in out:
        s["box"] = np.round(s["box"]).astype(int).tolist()
    return out


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


def _ocr_regions(dialogue_region, w, h):
    if not dialogue_region:
        raise ValueError("缺少该视频的台词区")
    return [
        (
            "dialogue",
            int(dialogue_region["left"] * w),
            int(dialogue_region["top"] * h),
            int(dialogue_region["right"] * w),
            int(dialogue_region["bottom"] * h),
        )
    ]


def default_jobs():
    """OCR 并行进程数默认值。

    <=2 核不并行（单进程多线程更快）；否则取 CPU 核数的一半（上限 8），
    每个子进程约分到 2 个推理线程，实测吞吐最优。
    """
    try:
        n = os.cpu_count() or 1
    except Exception:
        n = 1
    if n <= 2:
        return 1
    return max(2, min(n // 2, 8))


def _ocr_frame(backend, frame, regions, ocfg):
    """对一帧在台词区内做 OCR，返回过滤后的检测结果列表。"""
    min_score = float(ocfg["min_score"])
    min_area = float(ocfg["min_area"])
    max_chars = int(ocfg["max_text_chars"])
    require_jp = bool(ocfg["require_japanese"])
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
    return keep


def _ocr_scan_range(video, cfg, dialogue_region, w, h, start_frame, end_frame, step, sample_set, intra_threads=None):
    """顺序扫描 [start_frame, end_frame]，仅对 sample_set 中的帧做 OCR。"""
    ocfg = cfg["ocr"]
    backend = _make_backend(cfg, intra_threads=intra_threads)
    regions = _ocr_regions(dialogue_region, w, h)

    detections = {}
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % video)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    idx = start_frame
    while idx <= end_frame:
        ok, frame = cap.read()
        if not ok:
            break
        if idx in sample_set:
            keep = _ocr_frame(backend, frame, regions, ocfg)
            if keep:
                detections[idx] = keep
        idx += 1
    cap.release()
    return detections


def _split_ocr_chunks(sample_indices, jobs):
    """把采样帧索引按位置均分为 jobs 个连续子段。"""
    if jobs <= 1 or len(sample_indices) <= 1:
        return [sample_indices]
    n = max(1, min(jobs, len(sample_indices)))
    size, rem = divmod(len(sample_indices), n)
    chunks, pos = [], 0
    for i in range(n):
        k = size + (1 if i < rem else 0)
        chunks.append(sample_indices[pos:pos + k])
        pos += k
    return chunks


def _ocr_worker(payload):
    """子进程工作函数：扫描自己负责的帧段并做 OCR（每个子进程 1 个推理线程）。"""
    video, cfg, dialogue_region, w, h, sample_indices, step, intra_threads = payload
    if not sample_indices:
        return {}
    lo, hi = sample_indices[0], sample_indices[-1]
    return _ocr_scan_range(
        video, cfg, dialogue_region, w, h, lo, hi, step,
        set(sample_indices), intra_threads=intra_threads,
    )


def _collect_detections(video, cfg, dialogue_region, w, h, start_frame, end_frame, step, jobs=None):
    """抽帧 OCR。jobs>1 时用多进程并行，每个子进程只处理自己负责的帧段。"""
    jobs = default_jobs() if jobs is None else max(1, int(jobs))
    sample_indices = list(range(start_frame, end_frame + 1, step))
    if jobs <= 1 or len(sample_indices) <= 1:
        return _ocr_scan_range(
            video, cfg, dialogue_region, w, h,
            start_frame, end_frame, step, set(sample_indices),
        )

    chunks = _split_ocr_chunks(sample_indices, jobs)
    try:
        cpu_n = os.cpu_count() or 1
    except Exception:
        cpu_n = 1
    intra = max(1, cpu_n // jobs)  # 每个子进程分到的推理线程数
    payloads = [
        (video, cfg, dialogue_region, w, h, chunk, step, intra)
        for chunk in chunks if chunk
    ]
    detections = {}
    try:
        with ProcessPoolExecutor(max_workers=len(payloads)) as ex:
            futures = [ex.submit(_ocr_worker, p) for p in payloads]
            for fut in as_completed(futures):
                detections.update(fut.result())
    except Exception as exc:
        raise SystemExit("OCR 并行处理失败（可用 --jobs 1 顺序执行）: %s" % exc)
    return detections


def run_ocr(video, cfg, dialogue_region, force=False, jobs=None):
    seg_path = resolve_segments_path(video)
    if os.path.exists(seg_path) and not force:
        print("OCR 结果已存在，跳过（加 --force 强制重跑）")
        return load_segments(seg_path)

    w, h, fps, n = video_info(video)
    step = max(1, int(cfg["ocr"]["sample_step"]))
    jobs = default_jobs() if jobs is None else max(1, int(jobs))
    print("OCR 并行进程数: %d（--jobs 可调整）" % jobs)
    detections = _collect_detections(video, cfg, dialogue_region, w, h, 0, max(0, n - 1), step, jobs=jobs)

    segments = _build_segments(detections, step)
    save_segments(video, segments, fps)
    print(
        "OCR 完成：识别到 %d 段台词，唯一文本 %d"
        % (len(segments), len(set(s["text"] for s in segments)))
    )
    return segments


def run_ocr_range(
    video,
    cfg,
    dialogue_region,
    segment_id=None,
    start_seconds=None,
    end_seconds=None,
    padding_seconds=1.0,
    jobs=None,
):
    """重识别一个已有字幕片段或指定时间段，并回写 segments.json。

    局部 OCR 与全量 OCR 使用同一采样间隔，避免因加密采样捕捉逐字渲染的残片。
    """
    seg_path = resolve_segments_path(video)
    if not os.path.exists(seg_path):
        raise SystemExit("还没有 OCR 结果，请先运行: python run.py ocr <视频>")
    if padding_seconds < 0:
        raise SystemExit("--padding 不能小于 0")

    with open(seg_path, encoding="utf-8") as f:
        data = json.load(f)
    old_segments = data.get("segments", [])
    if not old_segments:
        raise SystemExit("segments.json 中没有可重识别的字幕段")

    w, h, fps, n = video_info(video)
    if fps <= 0:
        raise RuntimeError("无法读取视频帧率: %s" % video)

    if segment_id is not None:
        target = next((s for s in old_segments if s.get("id") == segment_id), None)
        if target is None:
            raise SystemExit("找不到编号为 %d 的字幕段" % segment_id)
        target_start, target_end = int(target["start"]), int(target["end"])
    else:
        if start_seconds is None or end_seconds is None:
            raise SystemExit("请指定 --segment，或同时指定 --start 与 --end")
        if start_seconds < 0 or end_seconds < start_seconds:
            raise SystemExit("时间范围无效：--start 必须 >= 0，且 --end 必须不早于 --start")
        target_start = int(round(start_seconds * fps))
        target_end = int(round(end_seconds * fps))

    target_start = max(0, min(target_start, max(0, n - 1)))
    target_end = max(target_start, min(target_end, max(0, n - 1)))
    padding_frames = int(round(padding_seconds * fps))
    scan_start = max(0, target_start - padding_frames)
    scan_end = min(max(0, n - 1), target_end + padding_frames)
    step = max(1, int(cfg["ocr"]["sample_step"]))

    detections = _collect_detections(video, cfg, dialogue_region, w, h, scan_start, scan_end, step, jobs=jobs)
    rescanned = _build_segments(detections, step)
    # 扩展范围只用于提供逐字显示的上下文；回写时只替换目标区间，避免影响邻句。
    replacements = [
        s for s in rescanned if s["end"] >= target_start and s["start"] <= target_end
    ]
    if not replacements:
        raise SystemExit("局部 OCR 未得到可替换的结果，原 segments.json 未修改")
    retained = [
        s for s in old_segments if s["end"] < target_start or s["start"] > target_end
    ]
    merged = _merge_near_duplicate_segments(retained + replacements, step)
    for i, segment in enumerate(merged):
        segment["id"] = i

    save_segments(video, merged, fps)
    print(
        "局部 OCR 完成：重识别 %.2f–%.2f 秒（扫描 %.2f–%.2f 秒，%d 帧采样），"
        "替换 %d 段为 %d 段"
        % (
            target_start / fps,
            target_end / fps,
            scan_start / fps,
            scan_end / fps,
            step,
            len(old_segments) - len(retained),
            len(replacements),
        )
    )
    return merged
