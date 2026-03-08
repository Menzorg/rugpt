#!/bin/bash
#
# Полное обновление движка RuGPT
# Использование: ./scripts/update.sh [сообщение]
#

set -e

MESSAGE="${1:-Обновление системы...}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUGPT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="/root/_rugpt"
DUMPS_DIR="${RUGPT_DIR}/dumps"
LATEST_DUMP=""

# БД
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-rugpt}"
DB_USER="${DB_USER:-postgres}"

# Webclient
WEBCLIENT_CONFIG_URL="http://10.0.0.1/api/config"

# Функция вывода информации о бекапах при ошибке
print_backup_info() {
    echo ""
    echo "📋 Для восстановления используйте:"
    if [ -n "$LATEST_DUMP" ]; then
        echo "   - Дамп БД: $LATEST_DUMP"
        echo "     psql -U $DB_USER -h $DB_HOST -d $DB_NAME -f $LATEST_DUMP"
    fi
    if [ -d "$BACKUP_DIR" ]; then
        echo "   - Бекап директории: $BACKUP_DIR"
    fi
    echo "   - Выключить maintenance: ./scripts/maintenance_off.sh"
}

echo "=========================================="
echo "🚀 Обновление RuGPT Engine"
echo "=========================================="
echo ""

# Загружаем .env для параметров БД
if [ -f "${RUGPT_DIR}/.env" ]; then
    while IFS='=' read -r key value; do
        [[ $key =~ ^#.*$ ]] && continue
        [[ -z $key ]] && continue
        value="${value%\"}"
        value="${value#\"}"
        export "$key=$value"
    done < "${RUGPT_DIR}/.env"
fi

# 1. Включить maintenance (если ещё не включён)
WC_MAINTENANCE=false
WC_RESPONSE=$(curl -sf "$WEBCLIENT_CONFIG_URL" 2>/dev/null || echo '{}')
if echo "$WC_RESPONSE" | grep -q '"maintenance":true'; then
    WC_MAINTENANCE=true
fi

if [ "$WC_MAINTENANCE" = true ]; then
    echo "📢 Шаг 1: Maintenance уже включён, пропускаю..."
else
    echo "📢 Шаг 1: Включаю режим технического обслуживания..."
    "${SCRIPT_DIR}/maintenance_on.sh" "$MESSAGE"

    # Ждём завершения текущих запросов
    echo "⏳ Ожидаю завершения текущих запросов (30 секунд)..."
    sleep 30
fi
echo ""

# 2. Дамп БД
echo "🗄️ Шаг 2: Создаю дамп базы данных..."
mkdir -p "${DUMPS_DIR}"
DUMP_FILE="${DUMPS_DIR}/db_dump_$(date +%Y%m%d_%H%M%S).sql"

export PGPASSWORD="$DB_PASSWORD"
if pg_dump -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" --clean --if-exists "$DB_NAME" > "$DUMP_FILE"; then
    LATEST_DUMP="$DUMP_FILE"
    echo "   ✅ Дамп создан: ${DUMP_FILE}"
else
    echo "   ❌ Ошибка при создании дампа БД!"
    echo "   Обновление прервано. Maintenance остаётся включённым."
    print_backup_info
    exit 1
fi
echo ""

# 3. Бекап директории
echo "📁 Шаг 3: Создаю бекап директории..."
if [ -d "${BACKUP_DIR}" ]; then
    echo "   Удаляю предыдущий бекап..."
    rm -rf "${BACKUP_DIR}"
fi

echo "   Копирую ${RUGPT_DIR} -> ${BACKUP_DIR}..."
if ! rsync -a \
    --exclude='logs/' \
    --exclude='dumps/' \
    --exclude='node_modules/' \
    --exclude='venv/' \
    "${RUGPT_DIR}/" "${BACKUP_DIR}/"; then
    echo "   ❌ Ошибка при создании бекапа!"
    echo "   Обновление прервано. Maintenance остаётся включённым."
    print_backup_info
    exit 1
fi
echo "   ✅ Бекап создан: ${BACKUP_DIR}"
echo ""

# 4. Git pull
echo "📥 Шаг 4: Получаю обновления из git..."
cd "${RUGPT_DIR}"
if ! git pull; then
    echo "   ❌ Ошибка git pull!"
    echo "   Maintenance остаётся включённым."
    print_backup_info
    exit 1
fi
echo ""

# 5. Перезапуск Engine (включает миграции)
echo "🔄 Шаг 5: Перезапуск Engine..."
cd "${RUGPT_DIR}"
./local_restart.sh engine

# Ждём готовности Engine
echo "⏳ Ожидаю готовности Engine..."
sleep 5

for i in {1..30}; do
    if curl -sf http://localhost:8100/api/v1/health > /dev/null 2>&1; then
        echo "✅ Engine готов!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ Engine не отвечает после 60 секунд!"
        echo "   Проверьте логи: screen -r rugpt-engine"
        echo "   Maintenance остаётся включённым."
        print_backup_info
        exit 1
    fi
    echo -n "."
    sleep 2
done
echo ""

# 6. Выключить maintenance
echo "📢 Шаг 6: Выключаю режим технического обслуживания..."
"${SCRIPT_DIR}/maintenance_off.sh"
echo ""

echo "=========================================="
echo "✅ Обновление завершено!"
echo "=========================================="
echo ""
echo "📋 Бекапы:"
echo "   - БД: ${LATEST_DUMP}"
echo "   - Директория: ${BACKUP_DIR}"
