#!/bin/bash
set -e

# Деплой LOCAL (MacBook) -> PROD (Proxmox LXC container)
#
# Usage:
#   ./deploy.sh           — полный деплой: rsync + pip install + restart engine
#   ./deploy.sh sync      — только rsync (без pip, без restart)
#
# Режим `sync` нужен для:
#   - первого деплоя (когда на проде ещё нет venv и .env, запускать restart нельзя)
#   - обновлений документации / конфигов / тестов без перезапуска движка
#
# SSH alias `rag-engine` должен быть настроен в ~/.ssh/config.

SERVER="rag-engine"
REMOTE_PATH="~/rugpt"
VENV_PIP="${REMOTE_PATH}/venv/bin/pip"

MODE="${1:-full}"

case "$MODE" in
  full|sync) ;;
  *)
    echo "Неизвестный режим: $MODE"
    echo "Usage: $0 [sync]"
    exit 1
    ;;
esac

echo "Режим: $MODE"
echo "Цель:  $SERVER:$REMOTE_PATH"

# === 1. rsync ===
rsync -avz --progress \
  -e "ssh" \
  --exclude 'venv' \
  --exclude '.git' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude 'uploads' \
  --exclude 'logs' \
  ./ ${SERVER}:${REMOTE_PATH}/

# === 1b. Mirror migrations exactly (--delete) ===
# Без этого переименование миграции оставляет на проде осиротевший файл,
# который migrate.sh потом накатит как «новый».
rsync -avz --progress --delete \
  -e "ssh" \
  ./src/engine/migrations/ ${SERVER}:${REMOTE_PATH}/src/engine/migrations/

if [ "$MODE" = "sync" ]; then
  echo "Sync завершён (pip install и restart пропущены)."
  exit 0
fi

# === 2. pip install (только full, только если requirements.txt изменился) ===
# ВНИМАНИЕ: хэш считается ПОСЛЕ rsync — сравниваем с тем что сейчас на проде.
# В двух последовательных `full` деплоях без изменений хэши совпадут.
# Но после `sync` файл уже обновлён — поэтому при первом `full` после `sync`
# требования могут не доустановиться. Безопаснее: прогнать руками или
# сделать `full` сразу после создания venv вручную.

OLD_REQ_HASH=$(ssh ${SERVER} "md5sum ${REMOTE_PATH}/requirements.txt 2>/dev/null | cut -d' ' -f1" || true)
NEW_REQ_HASH=$(md5sum requirements.txt 2>/dev/null | cut -d' ' -f1)

if [ -z "$OLD_REQ_HASH" ] || [ "$OLD_REQ_HASH" != "$NEW_REQ_HASH" ]; then
  echo "requirements.txt изменился или venv не инициализирован — устанавливаю зависимости..."
  ssh ${SERVER} "cd ${REMOTE_PATH} && ${VENV_PIP} install -r requirements.txt"
else
  echo "requirements.txt без изменений, пропускаю pip install."
fi

# === 3. Перезапуск движка (миграции + screen + health check) ===
echo "Перезапускаю Engine..."
ssh ${SERVER} "cd ${REMOTE_PATH} && ./local_restart.sh engine"

echo "Деплой завершён."
