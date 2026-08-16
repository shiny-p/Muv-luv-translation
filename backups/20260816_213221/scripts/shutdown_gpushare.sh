#!/bin/bash
# 恒源云实例一键关机启动器
# 用法: ./shutdown_gpushare.sh --login
#       ./shutdown_gpushare.sh --shutdown [--headless] [--instance-name 名称] [--console-url URL]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
NODE="$(command -v node 2>/dev/null || true)"
if [ -z "$NODE" ]; then
  for d in "$HOME"/.cache/codex-runtimes/*/dependencies/node/bin; do
    if [ -x "$d/node" ]; then NODE="$d/node"; break; fi
  done
fi
if [ -z "$NODE" ]; then
  echo "未找到 node 运行时。请先安装 Node.js,或在 PATH 中加入 node。" >&2
  exit 1
fi
exec "$NODE" "$DIR/shutdown_gpushare.js" "$@"
