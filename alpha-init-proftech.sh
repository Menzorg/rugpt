#!/bin/bash
#
# alpha-init-proftech.sh
# Инициализация первой альфа-организации: ООО Профессиональные технологии.
#
# Что создаёт:
#   - 1 организацию (timezone Europe/Samara) с org_context (4 направления + ИНН)
#   - 6 отделов: Отдел продаж, Отдел ИТ, Тендерный отдел, Логистика, Бухгалтерия, Маркетинг
#   - 15 visibility-правил: все отделы видят всех
#   - 10 ролей (supervisor + tools=[list_own_documents, get_own_tasks, analyze_image])
#     с промптами в src/engine/prompts/proftech_*.md
#   - 1 admin (Эдуард Панкратов, без отдела) + 10 сотрудников
#
# Пароли: у каждого юзера = часть email до '@'.
# Запускать из директории с .env (на проде — /home/rugpt/rugpt).
#
# Usage: ./alpha-init-proftech.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

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

# ============================================
# Org
# ============================================
ORG_NAME="ООО Профессиональные технологии"
ORG_SLUG="proftech"
ORG_TIMEZONE="Europe/Samara"
ORG_DESC=""
ORG_CONTEXT='В организации есть несколько направлений деятельности:
- поставка тяжелый силовых трансформаторов другим организация.
- работа с закупками (тендерами) по разным направлениям, 223фз преимущественно.
- оказание логистических услуг. В фирме есть грузовые машины.
- разработка программного обеспечения на основе искусственного интеллекта.

ИНН организации 6324057248. Указываю, так как много организаций с похожим названием'

# ============================================
# Юзеры: name, username, email, password (=email-до-@), is_admin, dept, head, role_code
# ============================================
# admin без отдела, без role_id
ADMIN_NAME="Панкратов Эдуард Рашитович"
ADMIN_USERNAME="pankratov_eduard"
ADMIN_EMAIL="doctor-pozitiv@yandex.ru"
ADMIN_PASSWORD="doctor-pozitiv"

# ============================================
# UUIDs
# ============================================
ORG_ID=$(cat /proc/sys/kernel/random/uuid)
ADMIN_ID=$(cat /proc/sys/kernel/random/uuid)

DEPT_SALES_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_IT_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_TENDER_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_LOGISTICS_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_ACCOUNTING_ID=$(cat /proc/sys/kernel/random/uuid)
DEPT_MARKETING_ID=$(cat /proc/sys/kernel/random/uuid)

ROLE_SALES_HEAD_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_DIRECT_SALES_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_IT_SUPPORT_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_AI_ML_DEV_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_ELEC_ENG_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_TENDER_MGR_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_LOGISTICS_HEAD_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_CHIEF_ACCOUNTANT_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_ACCOUNTANT_PRIMARY_ID=$(cat /proc/sys/kernel/random/uuid)
ROLE_STRATEGIC_COMMS_ID=$(cat /proc/sys/kernel/random/uuid)

U_MORKOVNIN_ID=$(cat /proc/sys/kernel/random/uuid)
U_AIRAPETIAN_ID=$(cat /proc/sys/kernel/random/uuid)
U_ERMAKOV_ID=$(cat /proc/sys/kernel/random/uuid)
U_YAKIMOV_ID=$(cat /proc/sys/kernel/random/uuid)
U_KUZYUKHIN_ID=$(cat /proc/sys/kernel/random/uuid)
U_KOZLOVA_ID=$(cat /proc/sys/kernel/random/uuid)
U_OSIPOV_ID=$(cat /proc/sys/kernel/random/uuid)
U_YATSENKO_ID=$(cat /proc/sys/kernel/random/uuid)
U_ANDREEVA_ID=$(cat /proc/sys/kernel/random/uuid)
U_MISHCHENKO_ID=$(cat /proc/sys/kernel/random/uuid)

echo -e "${YELLOW}=== Alpha Org Init: ${ORG_NAME} ===${NC}"

run_sql() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    else
        PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
    fi
}

run_sql_stdin() {
    if [ -n "$DB_PASSWORD" ]; then
        PGPASSWORD="$DB_PASSWORD" PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"
    else
        PGCLIENTENCODING=UTF8 psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME"
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

echo -e "${YELLOW}Проверка что org/admin ещё не созданы...${NC}"
EXISTING_SLUG=$(run_sql "SELECT slug FROM organizations WHERE slug = '$ORG_SLUG'" 2>/dev/null | grep -c "$ORG_SLUG" || true)
EXISTING_EMAIL=$(run_sql "SELECT email FROM users WHERE lower(email) = lower('$ADMIN_EMAIL')" 2>/dev/null | grep -ci "$ADMIN_EMAIL" || true)
if [ "$EXISTING_SLUG" -gt 0 ] || [ "$EXISTING_EMAIL" -gt 0 ]; then
    echo -e "${RED}Ошибка: org со slug=$ORG_SLUG или admin email=$ADMIN_EMAIL уже существует.${NC}"
    echo -e "${RED}Удалите вручную перед повторным запуском, либо смените параметры.${NC}"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# Python с bcrypt
if [ -x ./venv/bin/python ]; then
    PYTHON_BIN=./venv/bin/python
else
    PYTHON_BIN=python3
fi

echo -e "${YELLOW}Хеширование паролей (по email до '@')...${NC}"
# Передаём пароли через stdin, чтобы не лезть с шелл-эскейпом в bcrypt
read -r -d '' HASH_SCRIPT <<'PYEOF' || true
import bcrypt, sys
for line in sys.stdin:
    pwd = line.rstrip("\n").encode("utf-8")
    print(bcrypt.hashpw(pwd, bcrypt.gensalt(rounds=12)).decode("utf-8"))
PYEOF

# Порядок (важен для дальнейшего извлечения): admin, morkovnin, airapetian, ermakov, yakimov, kuzyukhin, kozlova, osipov, yatsenko, andreeva, mishchenko
HASHES=$(printf '%s\n' \
    "$ADMIN_PASSWORD" \
    "evgenymorkovnin" \
    "palenyi94" \
    "89033303665" \
    "mr.stone9999" \
    "nak70.96" \
    "elena.planetatender" \
    "s.osipov" \
    "Tanshib" \
    "buh" \
    "mishchenkom163" \
    | "$PYTHON_BIN" -c "$HASH_SCRIPT")

# shellcheck disable=SC2206
HASH_ARR=($HASHES)
H_ADMIN="${HASH_ARR[0]}"
H_MORKOVNIN="${HASH_ARR[1]}"
H_AIRAPETIAN="${HASH_ARR[2]}"
H_ERMAKOV="${HASH_ARR[3]}"
H_YAKIMOV="${HASH_ARR[4]}"
H_KUZYUKHIN="${HASH_ARR[5]}"
H_KOZLOVA="${HASH_ARR[6]}"
H_OSIPOV="${HASH_ARR[7]}"
H_YATSENKO="${HASH_ARR[8]}"
H_ANDREEVA="${HASH_ARR[9]}"
H_MISHCHENKO="${HASH_ARR[10]}"
echo -e "${GREEN}OK${NC}"

# ============================================
# Организация (с org_context через dollar-quoted строку)
# ============================================
echo -e "${YELLOW}Создание организации...${NC}"
run_sql_stdin <<SQL
INSERT INTO organizations (id, name, slug, description, timezone, org_context, is_active, created_at, updated_at)
VALUES (
    '$ORG_ID', '$ORG_NAME', '$ORG_SLUG', '$ORG_DESC', '$ORG_TIMEZONE',
    \$org_ctx\$$ORG_CONTEXT\$org_ctx\$,
    true, NOW(), NOW()
);
SQL
echo -e "${GREEN}OK: $ORG_NAME ($ORG_ID)${NC}"

# ============================================
# Отделы
# ============================================
echo -e "${YELLOW}Создание отделов...${NC}"
run_sql "
INSERT INTO departments (id, org_id, name, created_at, updated_at) VALUES
    ('$DEPT_SALES_ID',      '$ORG_ID', 'Отдел продаж',    NOW(), NOW()),
    ('$DEPT_IT_ID',         '$ORG_ID', 'Отдел ИТ',        NOW(), NOW()),
    ('$DEPT_TENDER_ID',     '$ORG_ID', 'Тендерный отдел', NOW(), NOW()),
    ('$DEPT_LOGISTICS_ID',  '$ORG_ID', 'Логистика',       NOW(), NOW()),
    ('$DEPT_ACCOUNTING_ID', '$ORG_ID', 'Бухгалтерия',     NOW(), NOW()),
    ('$DEPT_MARKETING_ID',  '$ORG_ID', 'Маркетинг',       NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK: 6 отделов${NC}"

# ============================================
# Visibility: все 15 пар (C(6,2))
# constraint: department_a_id < department_b_id → LEAST/GREATEST
# ============================================
echo -e "${YELLOW}Создание visibility-правил (все видят всех)...${NC}"
add_visibility_pair() {
    local a="$1" b="$2"
    run_sql "
    INSERT INTO department_visibility (org_id, department_a_id, department_b_id)
    VALUES ('$ORG_ID',
            LEAST('$a'::uuid, '$b'::uuid),
            GREATEST('$a'::uuid, '$b'::uuid));
    " > /dev/null
}
DEPTS=("$DEPT_SALES_ID" "$DEPT_IT_ID" "$DEPT_TENDER_ID" "$DEPT_LOGISTICS_ID" "$DEPT_ACCOUNTING_ID" "$DEPT_MARKETING_ID")
for ((i=0; i<${#DEPTS[@]}; i++)); do
    for ((j=i+1; j<${#DEPTS[@]}; j++)); do
        add_visibility_pair "${DEPTS[$i]}" "${DEPTS[$j]}"
    done
done
echo -e "${GREEN}OK: 15 пар${NC}"

# ============================================
# Роли (supervisor + tools)
# ============================================
echo -e "${YELLOW}Создание 10 ролей...${NC}"
TOOLS_JSON='["list_own_documents", "get_own_tasks", "analyze_image"]'
MODEL='google/gemma-4-31B-it'

insert_role() {
    local rid="$1" code="$2" name="$3" prompt_file="$4" desc="$5"
    run_sql "
    INSERT INTO roles (id, org_id, name, code, description, system_prompt, model_name,
                       agent_type, agent_config, tools, prompt_file, is_active, created_at, updated_at)
    VALUES ('$rid', '$ORG_ID', '$name', '$code',
            \$desc\$$desc\$desc\$,
            '$MODEL', 'supervisor', '{}', '$TOOLS_JSON', '$prompt_file',
            true, NOW(), NOW());
    " > /dev/null
    echo -e "${GREEN}OK role: $code${NC}"
}

insert_role "$ROLE_SALES_HEAD_ID"         "sales_head"           "Руководитель отдела продаж"               "proftech_sales_head.md"           "Вопросы по стратегии продаж, клиентам и сделкам по трансформаторному оборудованию"
insert_role "$ROLE_DIRECT_SALES_ID"       "direct_sales_manager" "Старший менеджер по прямым контрактам"    "proftech_direct_sales_manager.md" "Вопросы по прямым контрактам, переговорам и работе с B2B-клиентами"
insert_role "$ROLE_IT_SUPPORT_ID"         "it_support"           "IT специалист client support"             "proftech_it_support.md"           "Вопросы по ИТ-инфраструктуре, техподдержке и ИТ-сервисам компании"
insert_role "$ROLE_AI_ML_DEV_ID"          "ai_ml_dev"            "AI-ML разработчик"                        "proftech_ai_ml_dev.md"            "Вопросы по AI/ML-разработке и программным продуктам компании"
insert_role "$ROLE_ELEC_ENG_ID"           "electrical_engineer"  "Инженер-электрик"                         "proftech_electrical_engineer.md"  "Вопросы по техническим характеристикам трансформаторов и электрооборудования"
insert_role "$ROLE_TENDER_MGR_ID"         "tender_manager"       "Менеджер по тендерам"                     "proftech_tender_manager.md"       "Вопросы по тендерам, госзакупкам по 223-ФЗ и статусу заявок"
insert_role "$ROLE_LOGISTICS_HEAD_ID"     "logistics_head"       "Руководитель отдела логистики"            "proftech_logistics_head.md"       "Вопросы по доставке, маршрутам и логистике грузов"
insert_role "$ROLE_CHIEF_ACCOUNTANT_ID"   "chief_accountant"     "Главбух"                                  "proftech_chief_accountant.md"     "Вопросы по финансам, налогам и бухгалтерской отчётности"
insert_role "$ROLE_ACCOUNTANT_PRIMARY_ID" "accountant_primary"   "Бухгалтер первичной документации"         "proftech_accountant_primary.md"   "Вопросы по первичным документам: счетам, актам, накладным"
insert_role "$ROLE_STRATEGIC_COMMS_ID"    "strategic_comms"      "Менеджер по стратегическим коммуникациям" "proftech_strategic_comms.md"      "Вопросы по PR, коммуникациям и имиджу компании"

# ============================================
# Admin (без отдела, без role)
# ============================================
echo -e "${YELLOW}Создание admin: $ADMIN_NAME...${NC}"
run_sql "
INSERT INTO users (id, org_id, name, username, email, password_hash,
                   is_admin, is_active, created_at, updated_at)
VALUES ('$ADMIN_ID', '$ORG_ID', '$ADMIN_NAME', '$ADMIN_USERNAME', lower('$ADMIN_EMAIL'),
        '$H_ADMIN', true, true, NOW(), NOW());
" > /dev/null
echo -e "${GREEN}OK admin: $ADMIN_NAME ($ADMIN_ID)${NC}"

# ============================================
# Сотрудники
# insert_user <id> <name> <username> <email> <password_hash> <dept_id> <is_head> <role_id>
# ============================================
insert_user() {
    local uid="$1" name="$2" uname="$3" email="$4" hash="$5" dept="$6" is_head="$7" role="$8"
    run_sql "
    INSERT INTO users (id, org_id, name, username, email, password_hash,
                       role_id, department_id, is_head, is_admin, is_active, created_at, updated_at)
    VALUES ('$uid', '$ORG_ID', '$name', '$uname', lower('$email'), '$hash',
            '$role', '$dept', $is_head, false, true, NOW(), NOW());
    " > /dev/null
    echo -e "${GREEN}OK user: $name${NC}"
}

echo -e "${YELLOW}Создание сотрудников...${NC}"
insert_user "$U_MORKOVNIN_ID"   "Морковнин Евгений Борисович"     "morkovnin_evgeny"     "evgenymorkovnin@yandex.ru"     "$H_MORKOVNIN"   "$DEPT_SALES_ID"      true  "$ROLE_SALES_HEAD_ID"
insert_user "$U_AIRAPETIAN_ID"  "Айрапетян Михаил Камович"        "airapetian_mikhail"   "palenyi94@mail.ru"             "$H_AIRAPETIAN"  "$DEPT_SALES_ID"      false "$ROLE_DIRECT_SALES_ID"
insert_user "$U_ERMAKOV_ID"     "Ермаков Антон Сергеевич"         "ermakov_anton"        "89033303665@yandex.ru"         "$H_ERMAKOV"     "$DEPT_IT_ID"         true  "$ROLE_IT_SUPPORT_ID"
insert_user "$U_YAKIMOV_ID"     "Якимов Александр Борисович"      "yakimov_alexander"    "mr.stone9999@gmail.com"        "$H_YAKIMOV"     "$DEPT_IT_ID"         false "$ROLE_AI_ML_DEV_ID"
insert_user "$U_KUZYUKHIN_ID"   "Кузюхин Николай Алексеевич"      "kuzyukhin_nikolay"    "nak70.96@mail.ru"              "$H_KUZYUKHIN"   "$DEPT_IT_ID"         false "$ROLE_ELEC_ENG_ID"
insert_user "$U_KOZLOVA_ID"     "Козлова Елена Михайловна"        "kozlova_elena"        "elena.planetatender@yandex.ru" "$H_KOZLOVA"     "$DEPT_TENDER_ID"     true  "$ROLE_TENDER_MGR_ID"
insert_user "$U_OSIPOV_ID"      "Осипов Сергей Сергеевич"         "osipov_sergey"        "s.osipov@profteh.pro"          "$H_OSIPOV"      "$DEPT_LOGISTICS_ID"  true  "$ROLE_LOGISTICS_HEAD_ID"
insert_user "$U_YATSENKO_ID"    "Яценко Татьяна"                  "yatsenko_tatyana"     "Tanshib@mail.ru"               "$H_YATSENKO"    "$DEPT_ACCOUNTING_ID" true  "$ROLE_CHIEF_ACCOUNTANT_ID"
insert_user "$U_ANDREEVA_ID"    "Андреева Юлия Валерьевна"        "andreeva_yulia"       "buh@profteh.pro"               "$H_ANDREEVA"    "$DEPT_ACCOUNTING_ID" false "$ROLE_ACCOUNTANT_PRIMARY_ID"
insert_user "$U_MISHCHENKO_ID"  "Мищенко Михаил Эдуардович"       "mishchenko_mikhail"   "mishchenkom163@yandex.ru"      "$H_MISHCHENKO"  "$DEPT_MARKETING_ID"  true  "$ROLE_STRATEGIC_COMMS_ID"

# ============================================
# Сводка
# ============================================
echo ""
echo -e "${GREEN}=== Alpha-org создана ===${NC}"
echo ""
echo "Organization: $ORG_NAME"
echo "  id=$ORG_ID"
echo "  slug=$ORG_SLUG"
echo "  timezone=$ORG_TIMEZONE"
echo ""
echo "Admin (без отдела):"
echo "  $ADMIN_EMAIL — $ADMIN_NAME (password: $ADMIN_PASSWORD)"
echo ""
echo "Сотрудники (логин email / пароль до '@'):"
echo "  evgenymorkovnin@yandex.ru     evgenymorkovnin     Морковнин (head Отдел продаж)"
echo "  palenyi94@mail.ru             palenyi94           Айрапетян (Отдел продаж)"
echo "  89033303665@yandex.ru         89033303665         Ермаков (head Отдел ИТ)"
echo "  mr.stone9999@gmail.com        mr.stone9999        Якимов (Отдел ИТ)"
echo "  nak70.96@mail.ru              nak70.96            Кузюхин (Отдел ИТ)"
echo "  elena.planetatender@yandex.ru elena.planetatender Козлова (head Тендерный отдел)"
echo "  s.osipov@profteh.pro          s.osipov            Осипов (head Логистика)"
echo "  Tanshib@mail.ru               Tanshib             Яценко (head Бухгалтерия)"
echo "  buh@profteh.pro               buh                 Андреева (Бухгалтерия)"
echo "  mishchenkom163@yandex.ru      mishchenkom163      Мищенко (head Маркетинг)"
echo ""
echo "Роли (10 шт, supervisor agent_type, tools: list_own_documents, get_own_tasks, analyze_image):"
echo "  sales_head, direct_sales_manager, it_support, ai_ml_dev, electrical_engineer,"
echo "  tender_manager, logistics_head, chief_accountant, accountant_primary, strategic_comms"
echo ""
echo "Visibility: все 6 отделов видят друг друга (15 пар)"
echo ""
