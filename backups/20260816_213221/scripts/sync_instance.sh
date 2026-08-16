#!/bin/bash
# ============================================================
# 同步项目代码到恒源云实例（只同步代码，绝不覆盖实例专属配置）
# 用法: bash scripts/sync_instance.sh <SSH端口> <实例密码>
#   例: bash scripts/sync_instance.sh 36425 '<密码见 .env 实例1>'
#       bash scripts/sync_instance.sh 33993 '<密码见 .env 实例2>'
#
# ⚠️ 为什么必须用本脚本：
#   实例上的 config.yaml / .env 是实例专属调优配置（如 实例1 5060Ti 用
#   x264+ffmpeg、实例2 4060Ti 用 nvenc），本地仓库的 config.yaml 只是通用
#   默认值。直接 git pull / 解压 tar 包会把实例配置覆盖掉，导致无法开工。
#   本脚本打包时**强制排除 config.yaml、.env 及一切实例本地产物**，只更新代码。
# ============================================================
set -euo pipefail

PORT="${1:-}"
PASS="${2:-}"
if [ -z "$PORT" ] || [ -z "$PASS" ]; then
  echo "用法: bash scripts/sync_instance.sh <SSH端口> <实例密码>" >&2
  echo "  例: bash scripts/sync_instance.sh 36425 '<实例1密码>'  # 实例1(5060Ti)" >&2
  echo "      bash scripts/sync_instance.sh 33993 '<实例2密码>'  # 实例2(4060Ti)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="/root/Muv-luv-translation"

TARBALL=$(mktemp /tmp/mlt-sync-XXXXXX.tar.gz)
echo "========== 打包代码（排除 config.yaml/.env/视频/备份等） =========="
COPYFILE_DISABLE=1 tar -czf "$TARBALL" \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  --exclude='*.pyc' \
  --exclude='.claude' \
  --exclude='backups' \
  --exclude='*.mp4' \
  --exclude='*.zip' \
  --exclude='*.log' \
  --exclude='config.yaml' \
  --exclude='.env' \
  --exclude='*_output' \
  --exclude='ocr' \
  --exclude='translations.json' \
  --exclude='**/region_selector/frames' \
  -C "$PROJECT_DIR" .
echo "打包完成: $(du -h "$TARBALL" | cut -f1)  ->  $TARBALL"
echo "校验: config.yaml/.env 必须不在包内:"
tar tzf "$TARBALL" | grep -E '(^|/)config\.yaml$|(^|/)\.env$' && { echo "!! 打包异常：config.yaml/.env 混入，终止"; rm -f "$TARBALL"; exit 1; } || echo "  OK: 无 config.yaml / .env"

echo "========== 上传并解压到 $DEPLOY_DIR =========="
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" root@i-2.gpushare.com "mkdir -p $DEPLOY_DIR"
sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -P "$PORT" "$TARBALL" "root@i-2.gpushare.com:$DEPLOY_DIR/sync.tar.gz"
rm -f "$TARBALL"
sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -p "$PORT" root@i-2.gpushare.com "
  set -e
  cd $DEPLOY_DIR
  tar xzf sync.tar.gz
  rm -f sync.tar.gz
  echo '========== 同步完成 =========='
  echo 'config.yaml 是否保留(应显示实例调优值):'
  grep -E 'use_gpu|encoder|ffmpeg|hwaccel' config.yaml || echo '  !! config.yaml 缺失！'
  echo '代码已更新文件数:'
  ls -1 run.py core/*.py scripts/*.sh | wc -l
"
echo "========== 同步完成（实例上的 config.yaml / .env 未动） =========="
