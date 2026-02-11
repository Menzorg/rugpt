#!/bin/bash
#
# test-del.sh
# Удаляет все тестовые данные из RuGPT
#
# Usage: ./test-del.sh [--all]
#
# Options:
#   --all   Удалить ВСЕ данные (не только тестовые)
#

set -e

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Загружаем переменные окружения
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        # Пропускаем комментарии и пустые строки
        [[ $key =~ ^#.*$ ]] && continue
        [[ -z $key ]] && continue
        # Убираем кавычки если есть
        value="${value%\"}"
        value="${value#\"}"
        export "$key=$value"
    done < .env
fi

# Параметры БД
DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-rugpt}
DB_USER=${DB_USER:-postgres}
DB_PASSWORD=${DB_PASSWORD:-}

# Тестовые данные (для фильтрации)
TEST_ORG_SLUG="test-company"
TEST_ADMIN_EMAIL="admin@testcompany.ru"

# Режим удаления
DELETE_ALL=false
if [ "$1" == "--all" ]; then
    DELETE_ALL=true
fi

echo -e "${YELLOW}=== RuGPT Test Data Cleanup ===${NC}"
echo ""

# Функция для выполнения SQL
run_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    fi
}

# Функция для подсчета записей
count_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "$1" | tr -d ' '
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "$1" | tr -d ' '
    fi
}

# Проверяем подключение к БД
echo -e "${YELLOW}Проверка подключения к БД...${NC}"
if ! run_sql "SELECT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: не удалось подключиться к БД${NC}"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

if [ "$DELETE_ALL" == "true" ]; then
    echo -e "${RED}=== ВНИМАНИЕ: Удаление ВСЕХ данных ===${NC}"
    read -p "Вы уверены? Это удалит ВСЕ организации, пользователей, чаты и сообщения. (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Отменено."
        exit 0
    fi

    # Получаем ID всех организаций
    ORG_FILTER=""
else
    echo -e "${YELLOW}Режим: удаление только тестовых данных (slug='$TEST_ORG_SLUG')${NC}"

    # Получаем ID тестовой организации
    TEST_ORG_ID=$(count_sql "SELECT id FROM organizations WHERE slug = '$TEST_ORG_SLUG'" | head -1)

    if [ -z "$TEST_ORG_ID" ] || [ "$TEST_ORG_ID" == "" ]; then
        echo -e "${YELLOW}Тестовая организация не найдена. Нечего удалять.${NC}"
        exit 0
    fi

    echo "Найдена тестовая организация: $TEST_ORG_ID"
    ORG_FILTER="WHERE org_id = '$TEST_ORG_ID'"
fi

# Показываем что будет удалено
echo ""
echo -e "${YELLOW}Будет удалено:${NC}"

if [ "$DELETE_ALL" == "true" ]; then
    MSG_COUNT=$(count_sql "SELECT COUNT(*) FROM messages" | head -1)
    CHAT_COUNT=$(count_sql "SELECT COUNT(*) FROM chats" | head -1)
    ROLE_COUNT=$(count_sql "SELECT COUNT(*) FROM roles" | head -1)
    USER_COUNT=$(count_sql "SELECT COUNT(*) FROM users" | head -1)
    ORG_COUNT=$(count_sql "SELECT COUNT(*) FROM organizations" | head -1)
else
    MSG_COUNT=$(count_sql "SELECT COUNT(*) FROM messages WHERE chat_id IN (SELECT id FROM chats $ORG_FILTER)" | head -1)
    CHAT_COUNT=$(count_sql "SELECT COUNT(*) FROM chats $ORG_FILTER" | head -1)
    ROLE_COUNT=$(count_sql "SELECT COUNT(*) FROM roles $ORG_FILTER" | head -1)
    USER_COUNT=$(count_sql "SELECT COUNT(*) FROM users $ORG_FILTER" | head -1)
    ORG_COUNT=$(count_sql "SELECT COUNT(*) FROM organizations WHERE slug = '$TEST_ORG_SLUG'" | head -1)
fi

echo "  - Сообщений: $MSG_COUNT"
echo "  - Чатов:     $CHAT_COUNT"
echo "  - Ролей:     $ROLE_COUNT"
echo "  - Пользователей: $USER_COUNT"
echo "  - Организаций:   $ORG_COUNT"
echo ""

read -p "Продолжить удаление? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Отменено."
    exit 0
fi

echo ""
echo -e "${YELLOW}Удаление данных...${NC}"

if [ "$DELETE_ALL" == "true" ]; then
    # Удаляем ВСЕ в правильном порядке (FK constraints)
    echo "  Удаление сообщений..."
    run_sql "DELETE FROM messages;" > /dev/null

    echo "  Удаление чатов..."
    run_sql "DELETE FROM chats;" > /dev/null

    echo "  Удаление пользователей..."
    run_sql "DELETE FROM users;" > /dev/null

    echo "  Удаление ролей..."
    run_sql "DELETE FROM roles;" > /dev/null

    echo "  Удаление организаций..."
    run_sql "DELETE FROM organizations;" > /dev/null
else
    # Удаляем только тестовые данные
    echo "  Удаление сообщений..."
    run_sql "DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE org_id = '$TEST_ORG_ID');" > /dev/null

    echo "  Удаление чатов..."
    run_sql "DELETE FROM chats WHERE org_id = '$TEST_ORG_ID';" > /dev/null

    echo "  Удаление пользователей..."
    run_sql "DELETE FROM users WHERE org_id = '$TEST_ORG_ID';" > /dev/null

    echo "  Удаление ролей..."
    run_sql "DELETE FROM roles WHERE org_id = '$TEST_ORG_ID';" > /dev/null

    echo "  Удаление организации..."
    run_sql "DELETE FROM organizations WHERE id = '$TEST_ORG_ID';" > /dev/null
fi

echo ""
echo -e "${GREEN}=== Данные успешно удалены ===${NC}"
