#!/bin/bash
# ============================================================
# 恒源云实例一键初始化脚本（Muv-luv-translation）
# 支持两种模式：
#   - 本地模式：直接在实例上运行（默认）
#   - 远程模式：`--remote <host>:<port> <password> [<DASHSCOPE_API_KEY>] [<DEEPSEEK_API_KEY>]`
#     从本地机器上传项目并远程部署
# 用法:  bash init_instance.sh '<DASHSCOPE_API_KEY>' '<DEEPSEEK_API_KEY>'
#   - 两个 key 均可选；不传则跳过写 .env（稍后手动补）
#   - 若项目代码已预置到 DEPLOY_DIR，则跳过上传/克隆
#   - 幂等：可重复执行
# 环境: Ubuntu + NVIDIA 驱动（PyTorch 镜像通常自带）
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DEPLOY_DIR="/root/Muv-luv-translation"

# ============================================================
# 远程模式：从本地打包项目 -> scp 上传 -> SSH 远程执行
# ============================================================
if [ "${1:-}" = "--remote" ]; then
  REMOTE_TARGET="${2:-}"
  REMOTE_PASS="${3:-}"
  DASHSCOPE_KEY="${4:-}"
  DEEPSEEK_KEY="${5:-}"

  if [ -z "$REMOTE_TARGET" ] || [ -z "$REMOTE_PASS" ]; then
    echo "用法: bash init_instance.sh --remote <host>:<port> <password> [<DASHSCOPE_API_KEY>] [<DEEPSEEK_API_KEY>]"
    exit 1
  fi

  HOST="${REMOTE_TARGET%:*}"
  PORT="${REMOTE_TARGET#*:}"

  echo "========== [远程] 打包本地项目 =========="
  TARBALL=$(mktemp /tmp/muv-luv-project-XXXXXX.tar.gz)
  tar -czf "$TARBALL" \
    --exclude='.venv' \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    -C "$PROJECT_DIR" .

  echo "打包完成: $TARBALL  ($(du -h "$TARBALL" | cut -f1))"
  echo "目标: $HOST:$PORT"

  echo "========== [远程] 创建远程目录并上传项目 =========="
  sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" "root@$HOST" "mkdir -p $DEPLOY_DIR"
  sshpass -p "$REMOTE_PASS" scp -o StrictHostKeyChecking=no -P "$PORT" "$TARBALL" "root@$HOST:$DEPLOY_DIR/project.tar.gz"
  rm -f "$TARBALL"

  echo "========== [远程] 远程解压项目文件 =========="
  sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" "root@$HOST" bash -c "
    set -euo pipefail
    cd $DEPLOY_DIR
    echo '解压项目文件...'
    tar -xzf project.tar.gz
    rm -f project.tar.gz
    echo '项目文件已解压到 $DEPLOY_DIR'
    ls -la
  "

  echo "========== [远程] 在实例上执行部署脚本 =========="
  sshpass -p "$REMOTE_PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" "root@$HOST" \
    "cd $DEPLOY_DIR && bash scripts/init_instance.sh '$DASHSCOPE_KEY' '$DEEPSEEK_KEY'"

  echo "========== 远程部署完成 =========="
  exit 0
fi

# ============================================================
# 本地模式（在实例上执行）
# ============================================================
DASHSCOPE_KEY="${1:-}"
DEEPSEEK_KEY="${2:-}"

echo "========== [1/7] 安装系统依赖 =========="
apt-get update -y
apt-get install -y ffmpeg fonts-noto-cjk unzip python3-venv python3-pip >/dev/null 2>&1 || true
# 确保 ffmpeg 在 PATH（部分镜像自带/预装路径不同）
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!! ffmpeg 不在 PATH，尝试创建软链 ..."
  for f in /usr/bin/ffmpeg /opt/conda/bin/ffmpeg /usr/local/bin/ffmpeg; do
    if [ -x "$f" ]; then ln -sf "$f" /usr/local/bin/ffmpeg; echo "   -> $f"; break; fi
  done
fi
command -v ffmpeg >/dev/null 2>&1 || echo "!! 找不到 ffmpeg（后续 NVENC 校验将失败）"

echo "========== [2/7] 校验 NVENC =========="
if ffmpeg -hide_banner -encoders 2>/dev/null | grep h264_nvenc >/dev/null; then
  echo "OK: ffmpeg 支持 h264_nvenc"
else
  echo "!! h264_nvenc 不可用。请先: apt-get install -y ffmpeg"
  echo "   或更换含 NVIDIA 驱动 + NVENC ffmpeg 的镜像。"
  exit 1
fi

echo "========== [3/7] 项目代码检查 =========="
if [ -f "$DEPLOY_DIR/run.py" ] && [ -f "$DEPLOY_DIR/requirements.txt" ]; then
  echo "已检测到项目代码（$DEPLOY_DIR），使用本地/上传版本"
else
  echo "!! 未找到项目代码，请先上传项目文件到 $DEPLOY_DIR"
  echo "   本地执行: bash scripts/init_instance.sh --remote <host>:<port> <password> [key1] [key2]"
  exit 1
fi
cd "$DEPLOY_DIR"

echo "========== [4/7] Python 虚拟环境 =========="
if [ ! -x "$DEPLOY_DIR/.venv/bin/python" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# GPU OCR：替换 CPU 版 onnxruntime
# 恒源云默认镜像为 CUDA 12.4 + cuDNN 9.x，固定 onnxruntime-gpu 1.19.2（匹配 CUDA 12）；
# 注意：onnxruntime >= 1.23 需要 CUDA 13，在恒源云默认实例上 CUDA provider 会加载失败。
.venv/bin/pip install -q "onnxruntime-gpu==1.19.2" || echo "!! onnxruntime-gpu 安装失败（可后续手动处理）"

echo "========== [5/7] 写入 GPU 配置 =========="
# 定位中文字体（优先 Noto CJK，找不到则用 fc-list 兜底）
FONT_PATH=""
for f in /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
         /usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc; do
  if [ -f "$f" ]; then FONT_PATH="$f"; break; fi
done
if [ -z "$FONT_PATH" ] && command -v fc-list >/dev/null 2>&1; then
  FONT_PATH="$(fc-list ':lang=zh' -f '%{file}\n' 2>/dev/null | head -1)"
fi
if [ -z "$FONT_PATH" ]; then
  echo "!! 未找到中文字体，请安装 fonts-noto-cjk 后重跑，或在 config.yaml 的 render.font 指定"
fi
.venv/bin/python - "$FONT_PATH" <<'PY'
import sys, yaml
p = 'config.yaml'
cfg = yaml.safe_load(open(p, encoding='utf-8'))
cfg['ocr']['use_gpu'] = True
cfg['video']['encoder'] = 'nvenc'
cfg['video']['hwaccel'] = 'cuda'
cfg['render']['width'] = 1920
if sys.argv[1]:
    cfg['render']['font'] = sys.argv[1]
cfg['region']['fixed'] = [0.231, 0.773, 0.767, 0.959]
yaml.safe_dump(cfg, open(p, 'w', encoding='utf-8'), allow_unicode=True, sort_keys=False)
print('config.yaml -> GPU: use_gpu/nvenc/cuda/1920/固定区域/中文字体=%s' % sys.argv[1])
PY

echo "========== [6/7] API key（.env，可选） =========="
if [ -n "$DASHSCOPE_KEY" ] || [ -n "$DEEPSEEK_KEY" ]; then
  : > .env
  [ -n "$DASHSCOPE_KEY" ] && echo "DASHSCOPE_API_KEY=$DASHSCOPE_KEY" >> .env
  [ -n "$DEEPSEEK_KEY" ] && echo "DEEPSEEK_API_KEY=$DEEPSEEK_KEY" >> .env
  echo ".env 已写入（默认供应商为千问 DashScope，见 config.yaml）"
elif [ -f .env ]; then
  echo ".env 已存在，保留现有配置"
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
print('render.font =', cfg['render']['font'])
print('translation.provider =', cfg['translation']['provider'],
      '| model =', cfg['translation']['model'],
      '| key_env =', cfg['translation']['api_key_env'])
try:
    import onnxruntime as ort
    print('onnxruntime', ort.__version__)
    print('providers:', ort.get_available_providers())
except Exception as e:
    print('!! onnxruntime 导入失败:', e)
PY

echo "========== NVENC 实编码自检 =========="
ffmpeg -hide_banner -y -f lavfi -i testsrc=duration=0.3:size=320x240:rate=30 \
  -c:v h264_nvenc -f null - 2>&1 | tail -2

echo "=========================================="
echo "初始化完成！视频放到 /hy-tmp/ 后按 README 核心流程分步处理："
echo "  cd /root/Muv-luv-translation"
echo "  .venv/bin/python run.py cfr /hy-tmp/<视频>.mp4"
echo "  .venv/bin/python run.py ocr  /hy-tmp/<视频>_output/<视频>_cfr.mp4 --jobs 2"
echo "  .venv/bin/python run.py translate /hy-tmp/<视频>_output/<视频>_cfr.mp4"
echo "  .venv/bin/python run.py render /hy-tmp/<视频>_output/<视频>_cfr.mp4"
echo "（成品交付上传百度见 README「成品交付」章节）"
echo "=========================================="
