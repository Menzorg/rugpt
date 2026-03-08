#!/bin/bash

# Деплой LOCAL -> PROD (сервер 5090)
# Usage: ./deploy.sh

SERVER="5090"
REMOTE_PATH="~/rugpt"
VENV_PIP="${REMOTE_PATH}/venv/bin/pip"

echo "Деплой на PROD (${SERVER})..."

# Запомнить хэш requirements.txt на проде ДО деплоя
OLD_REQ_HASH=$(ssh ${SERVER} "md5sum ${REMOTE_PATH}/requirements.txt 2>/dev/null | cut -d' ' -f1")

rsync -avz --progress \
  -e "ssh" \
  --exclude 'venv' \
  --exclude '.git' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '__pycache__' \
  ./ ${SERVER}:${REMOTE_PATH}/

# Проверить хэш requirements.txt ПОСЛЕ деплоя
NEW_REQ_HASH=$(ssh ${SERVER} "md5sum ${REMOTE_PATH}/requirements.txt 2>/dev/null | cut -d' ' -f1")

if [ "$OLD_REQ_HASH" != "$NEW_REQ_HASH" ]; then
  echo "requirements.txt изменился, устанавливаю зависимости..."
  ssh ${SERVER} "cd ${REMOTE_PATH} && ${VENV_PIP} install -r requirements.txt"
else
  echo "requirements.txt без изменений, пропускаю pip install"
fi

echo "Деплой завершён!"
