import shutil

import cv2
import imageio_ffmpeg


def video_info(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("无法打开视频: %s" % path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if fps <= 0 or n <= 0:
        raise RuntimeError("无法读取视频信息: %s" % path)
    return w, h, fps, n


def get_ffmpeg(cfg=None):
    """返回 ffmpeg 可执行文件路径。

    优先级：config 的 video.ffmpeg > nvenc 时系统 PATH 里的 ffmpeg > 内置 ffmpeg。
    """
    cfg = cfg or {}
    vcfg = cfg.get("video") or {}
    custom = (vcfg.get("ffmpeg") or "").strip()
    if custom:
        return custom
    if (vcfg.get("encoder") or "x264") == "nvenc":
        exe = shutil.which("ffmpeg")
        if not exe:
            raise SystemExit(
                "nvenc 编码需要系统 ffmpeg（含 h264_nvenc）。"
                "请先安装并确认 `ffmpeg -encoders` 输出包含 h264_nvenc。"
            )
        return exe
    return imageio_ffmpeg.get_ffmpeg_exe()


def get_encoder(cfg=None):
    """返回视频编码器名：x264 -> libx264；nvenc -> h264_nvenc。"""
    cfg = cfg or {}
    enc = (cfg.get("video") or {}).get("encoder") or "x264"
    return {"x264": "libx264", "nvenc": "h264_nvenc"}.get(enc, "libx264")


def encoder_args(cfg, crf, preset):
    """按配置生成 ffmpeg 视频编码参数（CFR 与渲染共用）。"""
    if get_encoder(cfg) == "h264_nvenc":
        # NVENC 不支持 -crf：用 VBR + 质量目标 -cq；preset 取 p1~p7
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]


def write_cmd(video_path, width, height, fps, out, crf, preset, cfg=None):
    exe = get_ffmpeg(cfg)
    return [
        exe,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", "%dx%d" % (width, height),
        "-r", str(fps),
        "-i", "-",
        "-i", video_path,
        "-map", "0:v:0",
        "-map", "1:a:0?",
        *encoder_args(cfg, crf, preset),
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        out,
    ]


def get_hwaccel(cfg=None):
    """返回硬解方式：'cuda'（NVDEC/CUDA）或 ''（CPU 解码）。"""
    cfg = cfg or {}
    hw = (cfg.get("video") or {}).get("hwaccel") or ""
    return "cuda" if hw == "cuda" else ""


def hwaccel_decode_args(cfg=None):
    """返回放在 ffmpeg `-i` 之前的硬解参数（cuda/NVDEC），未启用时为空列表。"""
    if get_hwaccel(cfg) == "cuda":
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "nv12"]
    return []


def decode_frames_cmd(video, cfg=None, out_w=None, out_h=None):
    """硬解渲染用：ffmpeg 解码全部帧并以 BGR 输出到 stdout。

    - 未启用 hwaccel 时返回 None（调用方回退到 OpenCV 解码）。
    - 指定 out_w/out_h 时在 ffmpeg 内先 scale 到目标尺寸再输出，
      大幅减少管道数据量与 Python 侧开销（渲染的主要瓶颈）。
    """
    if get_hwaccel(cfg) != "cuda":
        return None
    cmd = [
        get_ffmpeg(cfg),
        "-hide_banner",
        "-loglevel", "error",
        "-hwaccel", "cuda",
        "-hwaccel_output_format", "nv12",
        "-i", video,
        "-an",
    ]
    if out_w and out_h:
        cmd += ["-vf", "scale=%d:%d" % (int(out_w), int(out_h))]
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    return cmd
