#!/bin/bash

# Синхронизация DEV (dev-nid) -> LOCAL
# Usage: ./sync.sh

VM_HOST="dev-nid"
REMOTE_PATH="/root/rugpt/"
LOCAL_PATH="$HOME/rugpt/"

echo "🔄 Синхронизация DEV (${VM_HOST}) -> LOCAL..."

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
