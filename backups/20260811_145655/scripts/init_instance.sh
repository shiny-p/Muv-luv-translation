#!/bin/bash
# ============================================================
# 恒源云实例一键初始化脚本（Muv-luv-translation）
# 用法:  bash init_instance.sh '<DASHSCOPE_API_KEY>' '<DEEPSEEK_API_KEY>'
#   - 两个 key 均可选；不传则跳过写 .env（稍后手动补）
#   - 幂等：可重复执行
# 环境: Ubuntu + NVIDIA 驱动（PyTorch 镜像通常自带）
# ============================================================
set -euo pipefail

DASHSCOPE_KEY="${1:-}"
DEEPSEEK_KEY="${2:-}"

echo "========== [1/7] 安装系统依赖 =========="
apt-get update -y
apt-get install -y ffmpeg fonts-noto-cjk unzip python3-venv python3-pip git curl >/dev/null 2>&1 || true

echo "========== [2/7] 校验 NVENC =========="
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc; then
  echo "OK: ffmpeg 支持 h264_nvenc"
else
  echo "!! h264_nvenc 不可用。若使用 PyTorch 官方镜像，请先:"
  echo "   apt-get install -y ffmpeg && ln -sf /usr/bin/ffmpeg /usr/local/bin/ffmpeg"
  echo "   或更换含 NVIDIA 驱动 ffmpeg 的镜像。"
  exit 1
fi

echo "========== [3/7] 获取项目代码 =========="
if [ ! -d /root/Muv-luv-translation/.git ]; then
  git clone https://github.com/shiny-p/Muv-luv-translation.git /root/Muv-luv-translation
else
  cd /root/Muv-luv-translation && git pull
fi
cd /root/Muv-luv-translation

echo "========== [4/7] Python 虚拟环境 =========="
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# GPU OCR：替换 CPU 版 onnxruntime（镜像若已装 onnxruntime-gpu 可跳过）
.venv/bin/pip install -q onnxruntime-gpu || echo "onnxruntime-gpu 安装失败（可后续手动处理）"

echo "========== [5/7] 写入 GPU 配置 =========="
.venv/bin/python - <<'PY'
import yaml
p = 'config.yaml'
cfg = yaml.safe_load(open(p, encoding='utf-8'))
cfg['ocr']['use_gpu'] = True
cfg['video']['encoder'] = 'nvenc'
cfg['video']['hwaccel'] = 'cuda'
cfg['render']['width'] = 1920
cfg['render']['font'] = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
cfg['region']['fixed'] = [0.231, 0.773, 0.767, 0.959]
yaml.safe_dump(cfg, open(p, 'w', encoding='utf-8'), allow_unicode=True, sort_keys=False)
print('config.yaml -> GPU: use_gpu/nvenc/cuda/1920/固定区域/中文字体')
PY

echo "========== [6/7] API key（.env，可选） =========="
if [ -n "$DASHSCOPE_KEY" ] || [ -n "$DEEPSEEK_KEY" ]; then
  : > .env
  [ -n "$DASHSCOPE_KEY" ] && echo "DASHSCOPE_API_KEY=$DASHSCOPE_KEY" >> .env
  [ -n "$DEEPSEEK_KEY" ] && echo "DEEPSEEK_API_KEY=$DEEPSEEK_KEY" >> .env
  echo ".env 已写入（默认供应商为千问 DashScope，见 config.yaml）"
else
  echo "未提供 API key，跳过 .env（处理前手动填写）"
fi

echo "========== [7/7] 验证 =========="
.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, '.')
from core.config import load_config
from core import cfr, config, ocr, regions, render, translate, video
cfg = load_config()
print('模块导入 OK')
print('ocr.use_gpu =', cfg['ocr']['use_gpu'],
      '| video.encoder =', cfg['video']['encoder'],
      '| video.hwaccel =', cfg['video']['hwaccel'],
      '| render.width =', cfg['render']['width'])
print('translation.provider =', cfg['translation']['provider'],
      '| model =', cfg['translation']['model'],
      '| key_env =', cfg['translation']['api_key_env'])
PY

echo "=========================================="
echo "初始化完成！视频放到 /hy-tmp/ 后按 README 核心流程分步处理："
echo "  cd /root/Muv-luv-translation"
echo "  .venv/bin/python run.py cfr /hy-tmp/<视频>.mp4"
echo "  .venv/bin/python run.py ocr  /hy-tmp/<视频>_output/<视频>_cfr.mp4 --jobs 2"
echo "  .venv/bin/python run.py translate /hy-tmp/<视频>_output/<视频>_cfr.mp4"
echo "  .venv/bin/python run.py render /hy-tmp/<视频>_output/<视频>_cfr.mp4"
echo "（成品交付上传百度见 README「成品交付」章节）"
echo "=========================================="
