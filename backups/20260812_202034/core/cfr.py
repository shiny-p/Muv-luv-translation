"""第0步：恒定帧率（CFR）转换。

把源视频（通常为可变帧率 VFR 录屏）用 ffmpeg 转成严格恒定帧率，
CFR 视频放在 <视频名>_output/ 文件夹内；按 cfr.keep_source 决定是否保留源视频
（默认 false=转换成功后删除源视频、保留 CFR，以节省磁盘空间）。
"""
import os
import shutil
import subprocess

from tqdm import tqdm

from .config import video_output_dir
from .video import video_info, get_ffmpeg, encoder_args, hwaccel_decode_args


def _target_fps(cfg, src_fps):
    """决定 CFR 目标帧率：cfr.fps > render.fps > 源视频帧率四舍五入。"""
    cfr = cfg.get("cfr") or {}
    render = cfg.get("render") or {}
    fps = float(cfr.get("fps") or 0)
    if fps <= 0:
        fps = float(render.get("fps") or 0)
    if fps <= 0:
        fps = round(src_fps)
    return max(1, fps)


def run_cfr(cfg, video, force=False):
    """转换源视频为 CFR，返回 CFR 视频路径（后续 OCR/翻译/渲染基于它）。"""
    if not os.path.exists(video):
        raise SystemExit("视频文件不存在: %s" % video)

    out_dir = video_output_dir(video)
    os.makedirs(out_dir, exist_ok=True)

    # 原始视频移入输出文件夹（工作区根目录保持干净；已在输出文件夹内则跳过）
    original = os.path.join(out_dir, os.path.basename(video))
    if os.path.abspath(video) != os.path.abspath(original):
        shutil.move(video, original)
        print("原视频已移入: %s" % original)
    video = original

    stem = os.path.splitext(os.path.basename(video))[0]
    suffix = (cfg.get("cfr") or {}).get("suffix") or "_cfr"
    out = os.path.join(out_dir, stem + suffix + ".mp4")
    if os.path.exists(out) and not force:
        print("CFR 视频已存在: %s（加 --force 重新转换）" % out)
        return out

    w, h, src_fps, n = video_info(video)
    fps = _target_fps(cfg, src_fps)
    cfr = cfg.get("cfr") or {}
    render = cfg.get("render") or {}
    preset = str(cfr.get("preset") or render.get("preset") or "fast")
    crf = str(int(cfr.get("crf") or render.get("crf") or 18))

    exe = get_ffmpeg(cfg)
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        *hwaccel_decode_args(cfg),
        "-i", video,
        "-vf", "fps=%s,setpts=N/(%s*TB)" % (fps, fps),
        "-af", "aresample=async=1",
        *encoder_args(cfg, crf, preset),
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        out,
    ]
    total = int(round(n * fps / src_fps)) if src_fps > 0 else n
    print("转换 CFR: %s (%dx%d，源 %s fps → 恒定 %s fps，约 %d 帧) → %s"
          % (video, w, h, round(src_fps, 2), fps, total, out))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    pbar = tqdm(total=total, desc="CFR 转换")
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    cur = int(us / 1_000_000 * fps)
                    pbar.n = min(total, cur)
                    pbar.refresh()
                except ValueError:
                    pass
            elif line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                    # 部分 ffmpeg 构建把 out_time_ms 实际输出为微秒
                    if ms > 1_000_000:
                        cur = int(ms / 1_000_000 * fps)
                    else:
                        cur = int(ms / 1000 * fps)
                    pbar.n = min(total, cur)
                    pbar.refresh()
                except ValueError:
                    pass
            elif line.startswith("progress=end"):
                break
    finally:
        pbar.close()
    err = proc.stderr.read()
    rc = proc.wait()
    if rc != 0:
        raise SystemExit("CFR 转换失败：%s" % err.strip() or "未知错误")
    print("CFR 视频: %s" % out)
    # 磁盘策略：默认删除源视频、保留 CFR（cfr.keep_source=true 可改为保留）
    keep = bool((cfg.get("cfr") or {}).get("keep_source"))
    if not keep and os.path.abspath(video) != os.path.abspath(out):
        try:
            os.remove(video)
            print("已删除源视频（保留 CFR，节省空间）: %s" % video)
        except OSError as e:
            print("源视频删除失败（可手动清理）: %s -> %s" % (video, e))
    return out
