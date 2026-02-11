#!/bin/bash
#
# test-init.sh
# Создает тестовую организацию и руководителя для RuGPT
#
# Usage: ./test-init.sh
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

# Тестовые данные
TEST_ORG_NAME="Тестовая Компания"
TEST_ORG_SLUG="test-company"
TEST_ORG_DESC="Тестовая организация для разработки"

TEST_ADMIN_NAME="Иван Петрович"
TEST_ADMIN_USERNAME="ivan_petrovich"
TEST_ADMIN_EMAIL="admin@testcompany.ru"
TEST_ADMIN_PASSWORD="test123"

# Генерируем UUID
ORG_ID=$(cat /proc/sys/kernel/random/uuid)
ADMIN_ID=$(cat /proc/sys/kernel/random/uuid)

echo -e "${YELLOW}=== RuGPT Test Data Initialization ===${NC}"
echo ""

# Функция для выполнения SQL
run_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    fi
}

# Проверяем подключение к БД
echo -e "${YELLOW}Проверка подключения к БД...${NC}"
if ! run_sql "SELECT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: не удалось подключиться к БД${NC}"
    echo "Проверьте настройки в .env"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# Проверяем что таблицы существуют
echo -e "${YELLOW}Проверка схемы БД...${NC}"
if ! run_sql "SELECT 1 FROM organizations LIMIT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: таблицы не созданы. Запустите миграции: ./migrate.sh${NC}"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# Проверяем нет ли уже тестовых данных
echo -e "${YELLOW}Проверка существующих данных...${NC}"
EXISTING=$(run_sql "SELECT email FROM users WHERE email = '$TEST_ADMIN_EMAIL'" 2>/dev/null | grep -c "$TEST_ADMIN_EMAIL" || true)
if [ "$EXISTING" -gt 0 ]; then
    echo -e "${YELLOW}Тестовый пользователь уже существует!${NC}"
    echo "Для пересоздания сначала запустите: ./test-del.sh"
    exit 0
fi
echo -e "${GREEN}OK${NC}"

# Хешируем пароль (bcrypt)
echo -e "${YELLOW}Генерация хеша пароля...${NC}"
PASSWORD_HASH=$(python3 -c "
import bcrypt
password = '$TEST_ADMIN_PASSWORD'.encode('utf-8')
salt = bcrypt.gensalt(rounds=12)
hashed = bcrypt.hashpw(password, salt)
print(hashed.decode('utf-8'))
")
echo -e "${GREEN}OK${NC}"

# Создаем организацию
echo -e "${YELLOW}Создание тестовой организации...${NC}"
run_sql "
INSERT INTO organizations (id, name, slug, description, is_active, created_at, updated_at)
VALUES (
    '$ORG_ID',
    '$TEST_ORG_NAME',
    '$TEST_ORG_SLUG',
    '$TEST_ORG_DESC',
    true,
    NOW(),
    NOW()
);
" > /dev/null
echo -e "${GREEN}OK: $TEST_ORG_NAME ($ORG_ID)${NC}"

# Создаем руководителя
echo -e "${YELLOW}Создание тестового руководителя...${NC}"
run_sql "
INSERT INTO users (id, org_id, name, username, email, password_hash, is_admin, is_active, created_at, updated_at)
VALUES (
    '$ADMIN_ID',
    '$ORG_ID',
    '$TEST_ADMIN_NAME',
    '$TEST_ADMIN_USERNAME',
    '$TEST_ADMIN_EMAIL',
    '$PASSWORD_HASH',
    true,
    true,
    NOW(),
    NOW()
);
" > /dev/null
echo -e "${GREEN}OK: $TEST_ADMIN_NAME ($ADMIN_ID)${NC}"

echo ""
echo -e "${GREEN}=== Тестовые данные созданы ===${NC}"
echo ""
echo "Организация:"
echo "  ID:   $ORG_ID"
echo "  Имя:  $TEST_ORG_NAME"
echo "  Slug: $TEST_ORG_SLUG"
echo ""
echo "Руководитель:"
echo "  ID:       $ADMIN_ID"
echo "  Имя:      $TEST_ADMIN_NAME"
echo "  Username: $TEST_ADMIN_USERNAME"
echo "  Email:    $TEST_ADMIN_EMAIL"
echo "  Пароль:   $TEST_ADMIN_PASSWORD"
echo "  isAdmin:  true"
echo ""
echo -e "${YELLOW}Для входа используйте:${NC}"
echo "  Email:    $TEST_ADMIN_EMAIL"
echo "  Password: $TEST_ADMIN_PASSWORD"
