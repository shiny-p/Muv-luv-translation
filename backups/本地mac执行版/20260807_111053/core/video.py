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


def get_ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()


def write_cmd(video_path, width, height, fps, out, crf, preset):
    exe = get_ffmpeg()
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
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        out,
    ]
