#!/bin/bash

# Деплой LOCAL -> PROD (сервер 5090)
# Usage: ./deploy.sh

SERVER="5090"
REMOTE_PATH="~/rugpt"

echo "🚀 Деплой на PROD (${SERVER})..."

rsync -avz --progress \
  -e "ssh" \
  --exclude 'venv' \
  --exclude '.git' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '__pycache__' \
  ./ ${SERVER}:${REMOTE_PATH}/

echo "✅ Деплой завершён!"
