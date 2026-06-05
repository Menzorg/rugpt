#!/bin/bash
# add-user-to-org.sh — добавить пользователя в существующую org.
# Отредактируй переменные ниже и запусти.

set -e

# ============================================
# Параметры — редактировать здесь
# ============================================
ORG_ID="266418bf-96d1-48f2-b86d-a294c2314804"
NAME=""
USERNAME=""
EMAIL=""
PASSWORD=""
DEPT_ID=""     # оставь пустым если не нужен
ROLE_CODE="" # оставь пустым если не нужен
# ============================================

if [ -f .env ]; then set -a; source .env; set +a; fi
DB_HOST=${DB_HOST:-localhost}; DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-rugpt};     DB_USER=${DB_USER:-postgres}

q() { PGPASSWORD="${DB_PASSWORD:-}" PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "$1"; }
run() { PGPASSWORD="${DB_PASSWORD:-}" PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"; }

PYTHON_BIN=$([ -x ./venv/bin/python ] && echo ./venv/bin/python || echo python3)
HASH=$(printf '%s' "$PASSWORD" | "$PYTHON_BIN" -c "
import bcrypt, sys
pwd = sys.stdin.read().strip().encode()
print(bcrypt.hashpw(pwd, bcrypt.gensalt(12)).decode())
")

ROLE_ID=""
if [ -n "$ROLE_CODE" ]; then
    ROLE_ID=$(q "SELECT id FROM roles WHERE org_id='$ORG_ID' AND code='$ROLE_CODE' AND is_active=true")
    [ -z "$ROLE_ID" ] && echo "Role '$ROLE_CODE' not found in org" && exit 1
fi

USER_ID=$(cat /proc/sys/kernel/random/uuid)

EXTRA_COLS=""; EXTRA_VALS=""
[ -n "$DEPT_ID"  ] && EXTRA_COLS="$EXTRA_COLS, department_id" && EXTRA_VALS="$EXTRA_VALS, '$DEPT_ID'"
[ -n "$ROLE_ID"  ] && EXTRA_COLS="$EXTRA_COLS, role_id"       && EXTRA_VALS="$EXTRA_VALS, '$ROLE_ID'"

run "
INSERT INTO users (id, org_id, name, username, email, password_hash, is_admin, is_active, created_at, updated_at$EXTRA_COLS)
VALUES ('$USER_ID', '$ORG_ID', '$NAME', '$USERNAME', lower('$EMAIL'), '$HASH', false, true, NOW(), NOW()$EXTRA_VALS);
"

echo "Done: $NAME | id=$USER_ID"
