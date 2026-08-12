import copy
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
SEGMENTS_PATH = os.path.join(ROOT, "ocr", "segments.json")
TRANSLATIONS_PATH = os.path.join(ROOT, "translations.json")
GLOSSARY_PATH = os.path.join(ROOT, "glossary.json")
OUTPUT_PATH = os.path.join(ROOT, "output.mp4")
ENV_PATH = os.path.join(ROOT, ".env")


def _load_env():
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

TEMPLATE = """\
# 视频日语字幕 → 简体中文 翻译工具 配置
# 视频路径通过命令行参数传入（例如: python run.py all 0_1_1.mp4），不写在此处。
# 修改保存后重跑会自动跳过已完成步骤。
# 台词区会按视频保存到 <视频名>_output/region.json；也可用 `python run.py regions <视频>` 单独检测
output: output.mp4             # 输出视频文件名（放在 <视频名>_output/ 文件夹内）

ocr:                           # ---- 文字识别 ----
  engine: rapidocr             # 目前可用: rapidocr(本地、免费、支持日文)
  lang: japan                  # 识别语言
  use_gpu: false               # true=用 onnxruntime-gpu(CUDA) 跑 OCR（需 NVIDIA 显卡，pip install onnxruntime-gpu）
  sample_step: 48              # 每多少帧抽 1 帧做 OCR（60fps 时约 800ms 一次）
  min_score: 0.5               # 识别置信度阈值，低于此值忽略
  min_area: 200                # 最小文字框面积(像素)，过滤噪点
  max_text_chars: 80           # 单条字幕最大字数
  require_japanese: true       # 只保留含日文字符的识别结果

translation:                   # ---- 文本翻译 ----
  provider: deepseek           # deepseek | openai | qwen | mock(本地测试用，不需要 key)
  base_url: ""                 # OpenAI 兼容接口地址；留空用供应商默认值
  api_key: ""                  # API key，也可以放到环境变量里
  api_key_env: DEEPSEEK_API_KEY
  model: ""                    # 模型名；留空用供应商默认值
  mode: script                 # script=整段剧本翻译（推荐，模型按上下文处理人名/歧义）；line=逐行翻译
  script_chunk_lines: 50       # 剧本模式每块台词数（越大上下文越全，但单请求更慢/更易超限）

region:                        # ---- 台词区 ----
  fixed: []                      # 固定台词区 [left,top,right,bottom]；非空时不再自动检测、直接应用（仍生成校验截图），单视频仍可用 region.json / --region 覆盖

video:                         # ---- 视频编码（CFR 与渲染共用）----
  encoder: x264                # x264=CPU 编码（内置 ffmpeg）；nvenc=GPU 编码（需系统 ffmpeg 含 h264_nvenc）
  ffmpeg: ""                   # 自定义 ffmpeg 可执行文件路径；留空自动（x264 用内置，nvenc 用系统 PATH 里的 ffmpeg）
  hwaccel: ""                  # 解码硬加速：""=CPU 解码；cuda=用 NVDEC/CUDA 硬解（需 NVIDIA 显卡 + 系统 ffmpeg）

cfr:                           # ---- 第0步：恒定帧率(CFR)转换 ----
  fps: 0                       # 目标帧率，0=自动（取 render.fps，否则源视频帧率四舍五入）
  crf: 18                      # x264 质量，越小越清晰
  preset: fast                 # x264 编码速度
  suffix: _cfr                 # CFR 文件后缀（<视频名><suffix>.mp4）
  keep_source: false           # CFR 转换成功后是否保留源视频；false=删除源视频、保留 CFR（省空间）

render:                        # ---- 渲染输出 ----
  append_height: 160           # 底部加高的像素
  append_bg: auto              # 条带底色：auto(匹配台词框暖灰) | black | #RRGGBB
  width: 0                     # 输出宽度，0=保持原分辨率；性能不足可填 1920/1280
  fps: 0                       # 输出帧率，0=保持原帧率
  crf: 18                      # x264 质量，越小越清晰
  preset: fast                 # x264 编码速度
  font: ""                     # 中文字体路径；留空自动查找系统字体
  font_color: "#FFFFFF"
  stroke: 3                    # 文字描边宽度(像素)
  stroke_color: "#000000"
  frame_margin: 0.03           # 单行宽度按画面可用宽度计算时的左右留白比例
  font_scale: 0.78             # 全局字号 = 所有字幕框高度中位数 × 此系数（统一字号防忽大忽小）
  test_frames: 0               # 调试用：只处理前 N 帧，0=全部
"""

DEFAULTS = {
    "input": "",
    "output": "output.mp4",
    "ocr": {
        "engine": "rapidocr",
        "lang": "japan",
        "use_gpu": False,
        "sample_step": 48,
        "min_score": 0.5,
        "min_area": 200,
        "max_text_chars": 80,
        "require_japanese": True,
    },
    "video": {
        "encoder": "x264",
        "ffmpeg": "",
        "hwaccel": "",
    },
    "region": {
        "fixed": [],
    },
    "translation": {
        "provider": "deepseek",
        "base_url": "",
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "",
        "mode": "script",
        "script_chunk_lines": 50,
    },
    "cfr": {
        "fps": 0,
        "crf": 18,
        "preset": "fast",
        "suffix": "_cfr",
        "keep_source": False,
    },
    "render": {
        "append_height": 160,
        "append_bg": "auto",
        "width": 0,
        "fps": 0,
        "crf": 18,
        "preset": "fast",
        "font": "",
        "font_color": "#FFFFFF",
        "stroke": 3,
        "stroke_color": "#000000",
        "frame_margin": 0.03,
        "font_scale": 0.78,
        "test_frames": 0,
    },
}


def init_config():
    if os.path.exists(CONFIG_PATH):
        return False
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    return True


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit("缺少 config.yaml，请先运行: python run.py init")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return deep_merge(DEFAULTS, cfg)


def resolve_api_key(cfg):
    key = (cfg.get("api_key") or "").strip()
    env = cfg.get("api_key_env") or ""
    if not key and env:
        key = os.environ.get(env, "") or ""
    if not key:
        raise SystemExit(
            "缺少 API key：请在 config.yaml 的 translation.api_key 填写，"
            "或设置环境变量 %s" % env
        )
    return key


def video_stem(video):
    return os.path.splitext(os.path.basename(video))[0]


def video_output_dir(video):
    stem = video_stem(video)
    parent = os.path.dirname(os.path.abspath(video))
    # 原始视频副本或 CFR 视频已在某个 *_output 文件夹内时，直接复用该文件夹，
    # 避免根据 furute_output/furute_cfr.mp4 再生成 furute_cfr_output/。
    if os.path.basename(parent).endswith("_output"):
        return parent
    return os.path.join(ROOT, stem + "_output")


def recent_output_dir():
    cand = []
    for d in os.listdir(ROOT):
        p = os.path.join(ROOT, d)
        if d.endswith("_output") and os.path.isdir(p):
            cand.append(p)
    if not cand:
        return None
    return max(cand, key=os.path.getmtime)


def resolve_segments_path(video=None):
    if video:
        p = os.path.join(video_output_dir(video), "segments.json")
        if os.path.exists(p):
            return p
    if os.path.exists(SEGMENTS_PATH):
        return SEGMENTS_PATH
    r = recent_output_dir()
    if r:
        p = os.path.join(r, "segments.json")
        if os.path.exists(p):
            return p
    return SEGMENTS_PATH


def resolve_translations_path(video=None):
    if video:
        p = os.path.join(video_output_dir(video), "translations.json")
        if os.path.exists(p):
            return p
    if os.path.exists(TRANSLATIONS_PATH):
        return TRANSLATIONS_PATH
    r = recent_output_dir()
    if r:
        p = os.path.join(r, "translations.json")
        if os.path.exists(p):
            return p
    return TRANSLATIONS_PATH
