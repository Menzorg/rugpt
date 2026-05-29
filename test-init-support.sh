#!/bin/bash
#
# test-init-support.sh
# Создаёт двух тестовых саппорт-операторов в орг RuGPT Support.
# Не зависит от test-init.sh — можно запускать поверх существующих данных.
# Идемпотентный: повторный запуск ничего не сломает.
#
# Usage: ./test-init-support.sh

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

DEFAULT_PASSWORD="test123"

# Орг RuGPT Support — создана миграцией 023 (или 026 в нашей версии)
RUGPT_SUPPORT_ORG_ID="00000001-0000-0000-0000-000000000000"

# Саппорт-операторы
OP_MARIA_NAME="Мария Помощникова"
OP_MARIA_USERNAME="support_maria"
OP_MARIA_EMAIL="maria@rugpt.support"

OP_ALEXEY_NAME="Алексей Помощников"
OP_ALEXEY_USERNAME="support_alexey"
OP_ALEXEY_EMAIL="alexey@rugpt.support"

run_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    else
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    fi
}

echo -e "${YELLOW}=== RuGPT Support Operators Init ===${NC}"

# Проверка БД
if ! run_sql "SELECT 1" > /dev/null 2>&1; then
    echo -e "${RED}Ошибка: не удалось подключиться к БД${NC}"
    exit 1
fi

# Проверка что орг RuGPT Support существует (создаётся миграцией 026_support_tickets.sql)
ORG_EXISTS=$(run_sql "SELECT 1 FROM organizations WHERE id = '$RUGPT_SUPPORT_ORG_ID'" 2>/dev/null | grep -c "1 row" || true)
if [ "$ORG_EXISTS" = "0" ]; then
    echo -e "${RED}Ошибка: орг RuGPT Support ($RUGPT_SUPPORT_ORG_ID) не найдена${NC}"
    echo -e "${RED}Запустите ./migrate.sh — миграция 026_support_tickets.sql её создаёт${NC}"
    exit 1
fi
echo -e "${GREEN}OK: орг RuGPT Support найдена${NC}"

# Хеш пароля (одинаковый для обоих операторов)
if [ -x ./venv/bin/python ]; then
    PYTHON_BIN=./venv/bin/python
else
    PYTHON_BIN=python3
fi

PASSWORD_HASH=$($PYTHON_BIN -c "
import bcrypt
pwd = '$DEFAULT_PASSWORD'.encode('utf-8')
print(bcrypt.hashpw(pwd, bcrypt.gensalt(rounds=12)).decode('utf-8'))
")

# Идемпотентный INSERT — ON CONFLICT (org_id, username) DO NOTHING.
# Если оператор уже есть — пропускаем, никаких ошибок.
run_sql "
INSERT INTO users (org_id, name, username, email, password_hash,
                   is_admin, is_system, is_active, created_at, updated_at)
VALUES
    ('$RUGPT_SUPPORT_ORG_ID', '$OP_MARIA_NAME', '$OP_MARIA_USERNAME',
     '$OP_MARIA_EMAIL', '$PASSWORD_HASH', false, false, true, NOW(), NOW()),
    ('$RUGPT_SUPPORT_ORG_ID', '$OP_ALEXEY_NAME', '$OP_ALEXEY_USERNAME',
     '$OP_ALEXEY_EMAIL', '$PASSWORD_HASH', false, false, true, NOW(), NOW())
ON CONFLICT (org_id, username) DO NOTHING;
" > /dev/null

# Сводка — сколько в итоге активных юзеров в орг
COUNT=$(run_sql "SELECT count(*) FROM users WHERE org_id = '$RUGPT_SUPPORT_ORG_ID' AND is_active = true AND is_system = false" 2>/dev/null | grep -E "^\s*[0-9]+" | head -1 | tr -d ' ' || echo "?")

echo -e "${GREEN}OK: $OP_MARIA_NAME, $OP_ALEXEY_NAME${NC}"
echo ""
echo -e "${GREEN}=== Саппорт-операторы готовы ===${NC}"
echo ""
echo "Орг: RuGPT Support (id=$RUGPT_SUPPORT_ORG_ID)"
echo "Активных не-системных юзеров в орг: $COUNT"
echo ""
echo "Логин для оператора (пароль у обоих: $DEFAULT_PASSWORD):"
echo "  $OP_MARIA_EMAIL    — $OP_MARIA_NAME"
echo "  $OP_ALEXEY_EMAIL — $OP_ALEXEY_NAME"