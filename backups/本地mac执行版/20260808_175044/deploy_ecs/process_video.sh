#!/usr/bin/env bash
# 在 ECS 上后台处理一个视频（用法: ./process_video.sh <视频文件>）
set -euo pipefail
APP_DIR="${APP_DIR:-/root/muv}"
VIDEO="$1"
if [ -z "$VIDEO" ]; then echo "用法: $0 <视频文件>"; exit 1; fi
cd "$APP_DIR"
LOG="process_$(basename "$VIDEO" | sed 's/\.[^.]*$//').log"
nohup .venv/bin/python run.py all "$VIDEO" > "$LOG" 2>&1 &
echo "已启动 PID=$!，日志: $APP_DIR/$LOG"
echo "查看进度: tail -f $APP_DIR/$LOG"
