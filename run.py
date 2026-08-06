import argparse
import os
import sys

from core import config
from core.ocr import run_ocr, run_ocr_range
from core.regions import detect_dialogue_region
from core.render import run_render
from core.translate import run_translate


def _video(arg):
    video = (arg or "").strip().strip('"')
    if not video:
        raise SystemExit("请指定视频文件: python run.py all <视频路径>")
    if not os.path.exists(video):
        raise SystemExit("视频文件不存在: %s" % video)
    return video


def _detect_region(video, cfg):
    region = detect_dialogue_region(video, cfg)
    config.persist_dialogue_region(region)
    return region


def main():
    ap = argparse.ArgumentParser(description="视频日语字幕 → 简体中文 替换工具")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init", help="生成 config.yaml 配置模板")
    p = sub.add_parser("regions", help="检测台词区并生成校验截图（写入 config.yaml）")
    p.add_argument("video", help="视频文件路径")
    p = sub.add_parser("ocr", help="第1步：检测台词区 + 字幕文字识别")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("ocr-range", help="局部重做 OCR（需已有 segments.json）")
    p.add_argument("video", help="视频文件路径")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--segment", type=int, help="segments.json 中要重识别的字幕段 id")
    target.add_argument("--start", type=float, help="要重识别范围的起始秒数（须配合 --end）")
    p.add_argument("--end", type=float, help="要重识别范围的结束秒数")
    p.add_argument("--padding", type=float, default=1.0, help="目标前后额外扫描秒数（默认 1）")
    p = sub.add_parser("translate", help="第2步：翻译为简体中文（可带视频名定位输出文件夹）")
    p.add_argument("video", nargs="?", help="视频文件路径（可选）")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("render", help="第3步：渲染（不传视频时用 OCR 结果里的视频）")
    p.add_argument("video", nargs="?", help="视频文件路径（可选）")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("all", help="一键执行 完整流程（自动检测台词区）")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--force", action="store_true")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    if args.cmd == "init":
        created = config.init_config()
        if created:
            print("已生成 config.yaml，请填写 API key 后运行: python run.py all <视频>")
        else:
            print("config.yaml 已存在，未覆盖")
        return

    cfg = config.load_config()
    if args.cmd == "regions":
        video = _video(getattr(args, "video", None))
        _detect_region(video, cfg)
    elif args.cmd == "ocr":
        video = _video(getattr(args, "video", None))
        _detect_region(video, cfg)
        run_ocr(video, cfg, force=args.force)
    elif args.cmd == "ocr-range":
        if args.start is not None and args.end is None:
            raise SystemExit("使用 --start 时必须同时指定 --end")
        if args.start is None and args.end is not None:
            raise SystemExit("--end 必须与 --start 一起使用")
        video = _video(args.video)
        run_ocr_range(
            video,
            cfg,
            segment_id=args.segment,
            start_seconds=args.start,
            end_seconds=args.end,
            padding_seconds=args.padding,
        )
    elif args.cmd == "translate":
        video = getattr(args, "video", None)
        run_translate(cfg, force=args.force, video=video)
    elif args.cmd == "render":
        video = getattr(args, "video", None)
        run_render(cfg, force=args.force, video=video)
    elif args.cmd == "all":
        video = _video(getattr(args, "video", None))
        _detect_region(video, cfg)
        run_ocr(video, cfg, force=args.force)
        run_translate(cfg, force=args.force, video=video)
        run_render(cfg, force=args.force, video=video)


if __name__ == "__main__":
    main()
