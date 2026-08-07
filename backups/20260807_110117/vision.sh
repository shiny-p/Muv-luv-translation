#!/bin/bash
# Claude Vision Skill 启动器：自动定位 node 运行时
# 用法: ./vision.sh <图片路径> [问题]
#       ./vision.sh --url <图片链接> [问题]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
NODE="$(command -v node 2>/dev/null || true)"
if [ -z "$NODE" ]; then
  for d in "$HOME"/.cache/codex-runtimes/*/dependencies/node/bin; do
    if [ -x "$d/node" ]; then NODE="$d/node"; break; fi
  done
fi
if [ -z "$NODE" ]; then
  echo "未找到 node 运行时。请先安装 Node.js，或在 PATH 中加入 node。" >&2
  exit 1
fi
exec "$NODE" "$DIR/vision.js" "$@"
