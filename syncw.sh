#!/bin/bash

# Синхронизация DEV (dev-nid:/home/wolflord/rugpt) -> LOCAL (домашняя машина wolflord)
# Запускается на домашней машине wolflord
# Usage: ./syncw.sh

VM_HOST="rugpt-dev"
REMOTE_PATH="/home/wolflord/rugpt/"
LOCAL_PATH="$HOME/rugpt/"

echo "🔄 Синхронизация DEV (${VM_HOST}:${REMOTE_PATH}) -> LOCAL (${LOCAL_PATH})..."

rsync -avz --progress --delete \
  -e "ssh" \
  --exclude '.git/' \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '*.env' \
  --exclude '.env.*' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude 'node_modules/' \
  "${VM_HOST}:${REMOTE_PATH}" "${LOCAL_PATH}"

echo "✅ Синхронизация завершена!"
