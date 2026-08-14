#!/usr/bin/env bash
# ============================================================
# Muv-luv-translation 阿里云 ECS 一键环境搭建脚本
# 用法（在 ECS 上以 root 或 sudo 运行）:
#   DEEPSEEK_API_KEY=sk-xxxx ./setup_ecs.sh [安装目录]
# 默认安装目录: /root/muv
# ============================================================
set -euo pipefail

APP_DIR="${1:-/root/muv}"
DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
export DEBIAN_FRONTEND=noninteractive

echo "==> 1/6 安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y curl git ca-certificates build-essential \
    libgl1 libglib2.0-0 fonts-noto-cjk
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl git gcc gcc-c++ make mesa-libGL glib2 \
    google-noto-sans-cjk-fonts || true
else
  echo "警告: 未识别 apt/dnf，请手工安装系统依赖后继续" >&2
fi

echo "==> 2/6 准备 Python 3.12"
PYTHON_BIN=""
for c in python3.12 /usr/local/bin/python3.12 /usr/bin/python3.12; do
  if command -v "$c" >/dev/null 2>&1; then PYTHON_BIN="$c"; break; fi
done
if [ -z "$PYTHON_BIN" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || true
    apt-get update -y
    apt-get install -y python3.12 python3.12-venv python3.12-dev
    PYTHON_BIN=/usr/bin/python3.12
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3.12 python3.12-pip python3.12-devel || true
    PYTHON_BIN=/usr/bin/python3.12
  fi
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "==> 未找到系统 Python 3.12，改用 uv 安装独立 Python"
  if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
  uv python install 3.12
  PYTHON_BIN="$(uv python find 3.12)"
fi
"$PYTHON_BIN" --version

echo "==> 3/6 获取项目源码"
mkdir -p "$APP_DIR"
if [ -f "$APP_DIR/run.py" ]; then
  echo "项目已存在: $APP_DIR"
elif command -v git >/dev/null 2>&1; then
  git clone --depth 1 https://github.com/shiny-p/Muv-luv-translation.git "$APP_DIR"
else
  echo "错误: 未安装 git 且项目不存在，请手工上传项目到 $APP_DIR" >&2
  exit 1
fi

echo "==> 4/6 创建虚拟环境并安装依赖"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> 5/6 预下载 OCR 模型（ModelScope，国内速度快）"
"$APP_DIR/.venv/bin/rapidocr" download_models >/dev/null 2>&1 || echo "模型下载失败可稍后手动: cd $APP_DIR && .venv/bin/rapidocr download_models"

echo "==> 6/6 写入配置（.env / config.yaml / 中文字体）"
if [ -n "$DEEPSEEK_API_KEY" ]; then
  cat > "$APP_DIR/.env" <<ENV
# 在此填入你的 API key
DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
ENV
  echo "已写入 $APP_DIR/.env (DEEPSEEK_API_KEY)"
else
  echo "注意: 未提供 DEEPSEEK_API_KEY，请稍后自行编辑 $APP_DIR/.env"
fi

# 生成 config.yaml（若不存在）
if [ ! -f "$APP_DIR/config.yaml" ]; then
  (cd "$APP_DIR" && .venv/bin/python run.py init)
fi

# 自动定位中文字体并写入 render.font（Linux 上没有 macOS 字体，必须指定）
FONT_PATH=""
for f in \
  /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
  /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
  /usr/share/fonts/google-noto-sans-cjk/NotoSansCJK-Regular.ttc \
  /usr/share/fonts/google-noto-sans-cjk/NotoSansCJK-Bold.ttc; do
  if [ -f "$f" ]; then FONT_PATH="$f"; break; fi
done
if [ -z "$FONT_PATH" ]; then
  FONT_PATH="$(find /usr/share/fonts -iname '*.tt[cf]' 2>/dev/null | xargs -r fc-list :file 2>/dev/null | grep -iE 'cjk|noto.*sc|source.han' | head -1 | cut -d: -f1)"
fi
if [ -n "$FONT_PATH" ]; then
  sed -i.bak "s|^  font:.*|  font: \"$FONT_PATH\"|" "$APP_DIR/config.yaml"
  echo "render.font = $FONT_PATH"
else
  echo "警告: 未找到中文字体，请安装 fonts-noto-cjk 后在 config.yaml 设置 render.font" >&2
fi

echo
echo "=================================================="
echo " 部署完成！项目目录: $APP_DIR"
echo " 下一步:"
echo "   1) 上传视频到 ECS，例如: scp 1.mp4 root@<ECS公网IP>:/root/muv/"
echo "   2) 执行: cd $APP_DIR && nohup .venv/bin/python run.py all 1.mp4 > run.log 2>&1 &"
echo "   3) 查看进度: tail -f $APP_DIR/run.log"
echo "   4) 完成后下载: scp -r root@<ECS公网IP>:/root/muv/1_output/ ./"
echo "=================================================="
