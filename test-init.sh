#!/bin/bash
#
# test-init.sh
# Создаёт тестовую организацию с отделами, правилами видимости и сотрудниками.
#
# Структура:
#   - 1 admin: Иван Петрович (без отдела)
#   - 3 отдела: Юристы, Маркетинг, Бухгалтеры
#   - 6 сотрудников (по 2 на отдел), head на каждый отдел
#   - 2 правила видимости: Юристы↔Маркетинг, Юристы↔Бухгалтеры
#     (Маркетинг и Бухгалтеры друг друга НЕ видят)
#   - 2 AI-роли: lawyer, accountant (promt-файлы в src/engine/prompts/)
#
# Usage: ./test-init.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Загружаем .env
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

DB_HOST=${DB_HOST:-localhost}
DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-rugpt}
DB_USER=${DB_USER:-postgres}
DB_PASSWORD=${DB_PASSWORD:-}

# Тестовые данные
TEST_ORG_NAME="Тестовая Компания"
TEST_ORG_SLUG="test-company"
TEST_ORG_DESC="Тестовая организация для разработки"
DEFAULT_PASSWORD="test123"

# Admin
ADMIN_NAME="Иван Петрович"
ADMIN_USERNAME="ivan_petrovich"
ADMIN_EMAIL="admin@testcompany.ru"

# Юристы
U_ANNA_NAME="Анна Юрьевна"
U_ANNA_USERNAME="anna_lawyer"
U_ANNA_EMAIL="anna@testcompany.ru"

U_DMITRY_NAME="Дмитрий Правов"
U_DMITRY_USERNAME="dmitry_lawyer"
U_DMITRY_EMAIL="dmitry@testcompany.ru"

# Маркетинг
U_OLEG_NAME="Олег Продажев"
U_OLEG_USERNAME="oleg_marketer"
U_OLEG_EMAIL="oleg@testcompany.ru"

U_ELENA_NAME="Елена Постова"
U_ELENA_USERNAME="elena_marketer"
U_ELENA_EMAIL="elena@testcompany.ru"

# Бухгалтеры
U_OLGA_NAME="Ольга Кассова"
U_OLGA_USERNAME="olga_accountant"
U_OLGA_EMAIL="olga@testcompany.ru"

U_SERGEY_NAME="Сергей Счётов"
U_SERGEY_USERNAME="sergey_accountant"
U_SERGEY_EMAIL="sergey@testcompany.ru"

# Саппорт-операторы (RuGPT Support org, создана миграцией 023)
RUGPT_SUPPORT_ORG_ID="00000001-0000-0000-0000-000000000000"
OP_MARIA_NAME="Мария Помощникова"
OP_MARIA_USERNAME="support_maria"
OP_MARIA_EMAIL="maria@rugpt.support"

OP_ALEXEY_NAME="Алексей Помощников"
OP_ALEXEY_USERNAME="support_alexey"
OP_ALEXEY_EMAIL="alexey@rugpt.support"

# UUIDs
ORG_ID=$(cat /proc/sys/kernel/random/uuid)
ADMIN_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_LAWYER_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_ACCOUNTANT_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_LAWYERS_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_MARKETING_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_ACCOUNTING_ID=$(cat /proc/sys/kernel/random/uuid)
U_ANNA_ID=$(cat /proc/sys/kernel/random/uuid)
U_DMITRY_ID=$(cat /proc/sys/kernel/random/uuid)
U_OLEG_ID=$(cat /proc/sys/kernel/random/uuid)
U_ELENA_ID=$(cat /proc/sys/kernel/random/uuid)
U_OLGA_ID=$(cat /proc/sys/kernel/random/uuid)
U_SERGEY_ID=$(cat /proc/sys/kernel/random/uuid)
OP_MARIA_ID=$(cat /proc/sys/kernel/random/uuid)
OP_ALEXEY_ID=$(cat /proc/sys/kernel/random/uuid)

echo -e "${YELLOW}=== RuGPT Test Data Initialization ===${NC}"
echo ""

run_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    fi
}

echo -e "${YELLOW}Проверка подключения к БД...${NC}"
if ! run_sql "SELECT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: не удалось подключиться к БД${NC}"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

echo -e "${YELLOW}Проверка схемы БД...${NC}"
if ! run_sql "SELECT 1 FROM organizations LIMIT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: таблицы не созданы. Запустите ./migrate.sh${NC}"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

echo -e "${YELLOW}Проверка существующих данных...${NC}"
EXISTING=$(run_sql "SELECT email FROM users WHERE email = '$ADMIN_EMAIL'" 2>/dev/null | grep -c "$ADMIN_EMAIL" || true)
if [ "$EXISTING" -gt 0 ]; then
    echo -e "${YELLOW}Тестовый admin уже существует. Запустите ./test-del.sh для пересоздания${NC}"
    exit 0
fi
echo -e "${GREEN}OK${NC}"

# Python с bcrypt
if [ -x ./venv/bin/python ]; then
    PYTHON_BIN=./venv/bin/python
else
    PYTHON_BIN=python3
fi

echo -e "${YELLOW}Хеширование общего пароля...${NC}"
PASSWORD_HASH=$($PYTHON_BIN -c "
import bcrypt
pwd = '$DEFAULT_PASSWORD'.encode('utf-8')
print(bcrypt.hashpw(pwd, bcrypt.gensalt(rounds=12)).decode('utf-8'))
")
echo -e "${GREEN}OK${NC}"

# ============================================
# Организация
# ============================================
echo -e "${YELLOW}Создание организации...${NC}"
run_sql "
INSERT INTO organizations (id, name, slug, description, is_active, created_at, updated_at)
VALUES ('$ORG_ID', '$TEST_ORG_NAME', '$TEST_ORG_SLUG', '$TEST_ORG_DESC', true, NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK: $TEST_ORG_NAME ($ORG_ID)${NC}"

# ============================================
# Роли
# ============================================
echo -e "${YELLOW}Создание роли lawyer...${NC}"
run_sql "
INSERT INTO roles (id, org_id, name, code, description, system_prompt, model_name,
                   agent_type, agent_config, tools, prompt_file, is_active, created_at, updated_at)
VALUES (
    '$ROLE_LAWYER_ID', '$ORG_ID',
    'Юрист', 'lawyer',
    'Корпоративный юрист-ассистент',
    'Вы — AI-юрист корпоративный.',
    'google/gemma-4-31B-it',
    'simple', '{}', '[]', 'lawyer.md',
    true, NOW(), NOW()
);
" > /dev/null
echo -e "${GREEN}OK: lawyer ($ROLE_LAWYER_ID)${NC}"

echo -e "${YELLOW}Создание роли accountant...${NC}"
run_sql "
INSERT INTO roles (id, org_id, name, code, description, system_prompt, model_name,
                   agent_type, agent_config, tools, prompt_file, is_active, created_at, updated_at)
VALUES (
    '$ROLE_ACCOUNTANT_ID', '$ORG_ID',
    'Бухгалтер', 'accountant',
    'Корпоративный бухгалтер-ассистент',
    'Вы — AI-бухгалтер.',
    'google/gemma-4-31B-it',
    'simple', '{}', '[]', 'accountant.md',
    true, NOW(), NOW()
);
" > /dev/null
echo -e "${GREEN}OK: accountant ($ROLE_ACCOUNTANT_ID)${NC}"

# ============================================
# Отделы
# ============================================
echo -e "${YELLOW}Создание отделов...${NC}"
run_sql "
INSERT INTO departments (id, org_id, name, created_at, updated_at) VALUES
    ('$DEPT_LAWYERS_ID',    '$ORG_ID', 'Юристы',     NOW(), NOW()),
    ('$DEPT_MARKETING_ID',  '$ORG_ID', 'Маркетинг',  NOW(), NOW()),
    ('$DEPT_ACCOUNTING_ID', '$ORG_ID', 'Бухгалтеры', NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK: 3 отдела${NC}"

# ============================================
# Правила видимости
# Таблица department_visibility требует department_a_id < department_b_id.
# LEAST/GREATEST на UUID в Postgres сравнивают лексикографически — подходит.
# ============================================
echo -e "${YELLOW}Создание правил видимости...${NC}"
run_sql "
INSERT INTO department_visibility (org_id, department_a_id, department_b_id)
VALUES
    ('$ORG_ID',
     LEAST('$DEPT_LAWYERS_ID'::uuid, '$DEPT_MARKETING_ID'::uuid),
     GREATEST('$DEPT_LAWYERS_ID'::uuid, '$DEPT_MARKETING_ID'::uuid)),
    ('$ORG_ID',
     LEAST('$DEPT_LAWYERS_ID'::uuid, '$DEPT_ACCOUNTING_ID'::uuid),
     GREATEST('$DEPT_LAWYERS_ID'::uuid, '$DEPT_ACCOUNTING_ID'::uuid));
" > /dev/null
echo -e "${GREEN}OK: 2 правила (Юристы↔Маркетинг, Юристы↔Бухгалтеры)${NC}"

# ============================================
# Admin (без отдела)
# ============================================
echo -e "${YELLOW}Создание admin: $ADMIN_NAME...${NC}"
run_sql "
INSERT INTO users (id, org_id, name, username, email, password_hash, is_admin, is_active, created_at, updated_at)
VALUES ('$ADMIN_ID', '$ORG_ID', '$ADMIN_NAME', '$ADMIN_USERNAME', '$ADMIN_EMAIL',
        '$PASSWORD_HASH', true, true, NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK: $ADMIN_NAME ($ADMIN_ID)${NC}"

# ============================================
# Сотрудники
# insert_user <id> <name> <username> <email> <dept_id|NULL> <is_head> <role_id|NULL>
# ============================================
insert_user() {
    local uid="$1" name="$2" uname="$3" email="$4" dept="$5" is_head="$6" role="$7"
    local role_sql="NULL"
    local dept_sql="NULL"
    [ "$role" != "NULL" ] && role_sql="'$role'"
    [ "$dept" != "NULL" ] && dept_sql="'$dept'"
    run_sql "
    INSERT INTO users (id, org_id, name, username, email, password_hash,
                       role_id, department_id, is_head, is_admin, is_active, created_at, updated_at)
    VALUES ('$uid', '$ORG_ID', '$name', '$uname', '$email', '$PASSWORD_HASH',
            $role_sql, $dept_sql, $is_head, false, true, NOW(), NOW());
    " > /dev/null
    echo -e "${GREEN}OK: $name${NC}"
}

echo -e "${YELLOW}Создание сотрудников...${NC}"
insert_user "$U_ANNA_ID"   "$U_ANNA_NAME"   "$U_ANNA_USERNAME"   "$U_ANNA_EMAIL"   "$DEPT_LAWYERS_ID"    true  "$ROLE_LAWYER_ID"
insert_user "$U_DMITRY_ID" "$U_DMITRY_NAME" "$U_DMITRY_USERNAME" "$U_DMITRY_EMAIL" "$DEPT_LAWYERS_ID"    false "$ROLE_LAWYER_ID"
insert_user "$U_OLEG_ID"   "$U_OLEG_NAME"   "$U_OLEG_USERNAME"   "$U_OLEG_EMAIL"   "$DEPT_MARKETING_ID"  true  "NULL"
insert_user "$U_ELENA_ID"  "$U_ELENA_NAME"  "$U_ELENA_USERNAME"  "$U_ELENA_EMAIL"  "$DEPT_MARKETING_ID"  false "NULL"
insert_user "$U_OLGA_ID"   "$U_OLGA_NAME"   "$U_OLGA_USERNAME"   "$U_OLGA_EMAIL"   "$DEPT_ACCOUNTING_ID" true  "$ROLE_ACCOUNTANT_ID"
insert_user "$U_SERGEY_ID" "$U_SERGEY_NAME" "$U_SERGEY_USERNAME" "$U_SERGEY_EMAIL" "$DEPT_ACCOUNTING_ID" false "$ROLE_ACCOUNTANT_ID"

# ============================================
# Саппорт-операторы (RuGPT Support org)
# Орг 00000001-... уже создана миграцией 023.
# Создаём двух обычных юзеров (is_system=false) для E2E тестирования саппорта.
# ============================================
echo -e "${YELLOW}Создание саппорт-операторов в орг RuGPT Support...${NC}"
run_sql "
INSERT INTO users (id, org_id, name, username, email, password_hash,
                   is_admin, is_system, is_active, created_at, updated_at)
VALUES
    ('$OP_MARIA_ID', '$RUGPT_SUPPORT_ORG_ID', '$OP_MARIA_NAME', '$OP_MARIA_USERNAME',
     '$OP_MARIA_EMAIL', '$PASSWORD_HASH', false, false, true, NOW(), NOW()),
    ('$OP_ALEXEY_ID', '$RUGPT_SUPPORT_ORG_ID', '$OP_ALEXEY_NAME', '$OP_ALEXEY_USERNAME',
     '$OP_ALEXEY_EMAIL', '$PASSWORD_HASH', false, false, true, NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK: $OP_MARIA_NAME, $OP_ALEXEY_NAME${NC}"

# ============================================
# Тестовые документы -> upload + ingest
# ============================================
echo -e "${YELLOW}Загрузка и индексация документов из test-docs...${NC}"
"$PYTHON_BIN" ./test-ingest.py "$ORG_ID" "$ADMIN_ID"
echo -e "${GREEN}OK: документы из test-docs отправлены в ingest${NC}"

# ============================================
# Сводка
# ============================================
echo ""
echo -e "${GREEN}=== Тестовые данные созданы ===${NC}"
echo ""
echo "Организация:  $TEST_ORG_NAME (id=$ORG_ID, slug=$TEST_ORG_SLUG)"
echo ""
echo "Отделы:"
echo "  Юристы      id=$DEPT_LAWYERS_ID"
echo "  Маркетинг   id=$DEPT_MARKETING_ID"
echo "  Бухгалтеры  id=$DEPT_ACCOUNTING_ID"
echo ""
echo "Правила видимости:"
echo "  Юристы ↔ Маркетинг"
echo "  Юристы ↔ Бухгалтеры"
echo "  (Маркетинг и Бухгалтеры друг друга НЕ видят)"
echo ""
echo "Роли:"
echo "  lawyer      id=$ROLE_LAWYER_ID   prompt=lawyer.md"
echo "  accountant  id=$ROLE_ACCOUNTANT_ID   prompt=accountant.md"
echo ""
echo "Сотрудники (пароль у всех: $DEFAULT_PASSWORD):"
echo "  $ADMIN_EMAIL  — $ADMIN_NAME  [admin] (без отдела)"
echo "  $U_ANNA_EMAIL  — $U_ANNA_NAME  [head] Юристы, lawyer"
echo "  $U_DMITRY_EMAIL  — $U_DMITRY_NAME  Юристы, lawyer"
echo "  $U_OLEG_EMAIL  — $U_OLEG_NAME  [head] Маркетинг"
echo "  $U_ELENA_EMAIL  — $U_ELENA_NAME  Маркетинг"
echo "  $U_OLGA_EMAIL  — $U_OLGA_NAME  [head] Бухгалтеры, accountant"
echo "  $U_SERGEY_EMAIL  — $U_SERGEY_NAME  Бухгалтеры, accountant"
echo ""
echo "Саппорт-операторы (орг RuGPT Support, для E2E):"
echo "  $OP_MARIA_EMAIL  — $OP_MARIA_NAME"
echo "  $OP_ALEXEY_EMAIL  — $OP_ALEXEY_NAME"
echo ""
echo -e "${YELLOW}Вход для admin:${NC}"
echo "  Email:    $ADMIN_EMAIL"
echo "  Password: $DEFAULT_PASSWORD"
echo ""
echo -e "${YELLOW}Вход для саппорт-оператора:${NC}"
echo "  Email:    $OP_MARIA_EMAIL"
echo "  Password: $DEFAULT_PASSWORD"
