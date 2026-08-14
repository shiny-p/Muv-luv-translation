import argparse
import os
import sys

from core import config
from core.cfr import run_cfr
from core.ocr import run_ocr, run_ocr_range
from core.regions import detect_dialogue_region, load_dialogue_region, save_dialogue_region
from core.render import run_render
from core.translate import run_translate


def _video(arg):
    video = (arg or "").strip().strip('"')
    if not video:
        raise SystemExit("请指定视频文件: python run.py all <视频路径>")
    if not os.path.exists(video):
        raise SystemExit("视频文件不存在: %s" % video)
    return video


def _parse_region(video, value):
    try:
        left, top, right, bottom = (float(x.strip()) for x in value.split(","))
    except (AttributeError, ValueError):
        raise SystemExit("--region 格式应为 left,top,right,bottom，例如 0.223,0.774,0.746,0.923")
    try:
        return save_dialogue_region(
            video,
            {"left": left, "top": top, "right": right, "bottom": bottom},
        )
    except ValueError as exc:
        raise SystemExit("--region 无效：%s" % exc)


def _get_region(video, cfg, redetect=False, region_text=None):
    if region_text:
        return _parse_region(video, region_text)
    if redetect:
        return detect_dialogue_region(video, cfg)
    return load_dialogue_region(video) or detect_dialogue_region(video, cfg)


def main():
    ap = argparse.ArgumentParser(description="视频日语字幕 → 简体中文 替换工具")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init", help="生成 config.yaml 配置模板")
    p = sub.add_parser("cfr", help="第0步：源视频转恒定帧率(CFR)，原始视频与CFR视频一并放入输出文件夹")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("regions", help="检测台词区并生成校验截图（写入该视频的 region.json）")
    p.add_argument("video", help="视频文件路径")
    p = sub.add_parser("ocr", help="第1步：检测台词区 + 字幕文字识别")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--force", action="store_true")
    p.add_argument("--redetect-region", action="store_true", help="忽略已有 region.json，重新检测台词区")
    p.add_argument("--region", help="手工指定并保存台词区：left,top,right,bottom（相对坐标）")
    p.add_argument("--jobs", type=int, default=None, help="OCR 并行进程数（默认=CPU核数，上限8；1=顺序）")
    p = sub.add_parser("ocr-range", help="局部重做 OCR（需已有 segments.json）")
    p.add_argument("video", help="视频文件路径")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--segment", type=int, help="segments.json 中要重识别的字幕段 id")
    target.add_argument("--start", type=float, help="要重识别范围的起始秒数（须配合 --end）")
    p.add_argument("--end", type=float, help="要重识别范围的结束秒数")
    p.add_argument("--padding", type=float, default=1.0, help="目标前后额外扫描秒数（默认 1）")
    p.add_argument("--redetect-region", action="store_true", help="忽略已有 region.json，重新检测台词区")
    p.add_argument("--region", help="手工指定并保存台词区：left,top,right,bottom（相对坐标）")
    p.add_argument("--jobs", type=int, default=None, help="OCR 并行进程数（默认=CPU核数，上限8；1=顺序）")
    p = sub.add_parser("translate", help="第2步：翻译为简体中文（可带视频名定位输出文件夹）")
    p.add_argument("video", nargs="?", help="视频文件路径（可选）")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("render", help="第3步：渲染（不传视频时用 OCR 结果里的视频）")
    p.add_argument("video", nargs="?", help="视频文件路径（可选）")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("all", help="一键执行 完整流程（自动检测台词区）")
    p.add_argument("video", help="视频文件路径")
    p.add_argument("--force", action="store_true")
    p.add_argument("--redetect-region", action="store_true", help="忽略已有 region.json，重新检测台词区")
    p.add_argument("--region", help="手工指定并保存台词区：left,top,right,bottom（相对坐标）")
    p.add_argument("--jobs", type=int, default=None, help="OCR 并行进程数（默认=CPU核数，上限8；1=顺序）")

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
    if args.cmd == "cfr":
        video = _video(args.video)
        run_cfr(cfg, video, force=args.force)
    elif args.cmd == "regions":
        video = _video(getattr(args, "video", None))
        detect_dialogue_region(video, cfg)
    elif args.cmd == "ocr":
        video = _video(getattr(args, "video", None))
        region = _get_region(video, cfg, args.redetect_region, args.region)
        run_ocr(video, cfg, region, force=args.force, jobs=getattr(args, "jobs", None))
    elif args.cmd == "ocr-range":
        if args.start is not None and args.end is None:
            raise SystemExit("使用 --start 时必须同时指定 --end")
        if args.start is None and args.end is not None:
            raise SystemExit("--end 必须与 --start 一起使用")
        video = _video(args.video)
        region = _get_region(video, cfg, args.redetect_region, args.region)
        run_ocr_range(
            video,
            cfg,
            region,
            segment_id=args.segment,
            start_seconds=args.start,
            end_seconds=args.end,
            padding_seconds=args.padding,
            jobs=getattr(args, "jobs", None),
        )
    elif args.cmd == "translate":
        video = getattr(args, "video", None)
        run_translate(cfg, force=args.force, video=video)
    elif args.cmd == "render":
        video = getattr(args, "video", None)
        run_render(cfg, force=args.force, video=video)
    elif args.cmd == "all":
        video = _video(getattr(args, "video", None))
        video = run_cfr(cfg, video, force=args.force)  # 第0步：转 CFR，返回 CFR 视频路径
        region = _get_region(video, cfg, args.redetect_region, args.region)
        run_ocr(video, cfg, region, force=args.force, jobs=getattr(args, "jobs", None))
        run_translate(cfg, force=args.force, video=video)
        run_render(cfg, force=args.force, video=video)


if __name__ == "__main__":
    main()
