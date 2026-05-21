#!/bin/bash
# Перезапуск RuGPT Engine

# Получаем параметр
SERVICE="$1"

case "$SERVICE" in
    "engine")
        echo "🔄 Перезапуск Engine..."
        ;;
    "")
        echo "🔄 Полный перезапуск RuGPT..."
        ;;
    *)
        echo "❌ Неверный параметр. Использование:"
        echo "  ./local_restart.sh              - полный перезапуск"
        echo "  ./local_restart.sh engine       - только engine"
        exit 1
        ;;
esac

# Переходим в директорию проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Адрес для health-check и вывода берём из .env — движок биндится на API_HOST,
# поэтому проверять надо именно его, а не localhost (иначе health-check врёт).
API_HOST=$(grep -E '^API_HOST=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2)
API_PORT=$(grep -E '^API_PORT=' "$SCRIPT_DIR/.env" 2>/dev/null | cut -d= -f2)
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8100}"
# 0.0.0.0 нельзя курлить напрямую — бьём в loopback.
CURL_HOST="$API_HOST"
[ "$CURL_HOST" = "0.0.0.0" ] && CURL_HOST="127.0.0.1"
HEALTH_URL="http://${CURL_HOST}:${API_PORT}/api/v1/health"

# Функция остановки Engine
stop_engine() {
    echo "🛑 Останавливаем Engine..."

    # Останавливаем screen
    screen -S rugpt-engine -X quit 2>/dev/null || true
    sleep 2

    # Останавливаем uvicorn процессы (только свои, не Docker)
    echo "🛑 Останавливаем uvicorn процессы..."
    pkill -15 -u "$(whoami)" -f "uvicorn.*src.engine.app:app" 2>/dev/null || true
    pkill -15 -u "$(whoami)" -f "python.*src.engine.run" 2>/dev/null || true
    sleep 2

    # Проверяем, остались ли процессы
    if pgrep -u "$(whoami)" -f "uvicorn.*src.engine.app:app|python.*src.engine.run" > /dev/null; then
        echo "  ⚠️ Применяем принудительную остановку..."
        pkill -9 -u "$(whoami)" -f "uvicorn.*src.engine.app:app" 2>/dev/null || true
        pkill -9 -u "$(whoami)" -f "python.*src.engine.run" 2>/dev/null || true
        sleep 1
    fi

    echo "  ✅ Engine остановлен"
}

# Функция запуска Engine
start_engine() {
    echo "🚀 Запускаем RuGPT Engine..."

    # Выполняем миграции
    echo "🗂️ Выполнение миграций базы данных..."
    source venv/bin/activate
    python -c "
import asyncio
from src.engine.migrations.migrate import run_migrations
asyncio.run(run_migrations())
print('✅ Миграции выполнены')
" 2>/dev/null || echo "⚠️ Миграции уже выполнены или ошибка"

    # Запускаем Engine в screen
    screen -dmS rugpt-engine bash -c "cd $SCRIPT_DIR && source venv/bin/activate && python -m src.engine.run; exec bash"

    # Ждём запуска: опрашиваем health до ~60с. Старт тяжёлый (импорты langchain,
    # пре-варм токенайзера, коннект к Kafka), один sleep 5 + одиночный curl врал.
    echo "⏳ Ожидаем запуска Engine (${HEALTH_URL})..."
    HEALTHY=false
    for _ in $(seq 1 30); do
        if curl -s "$HEALTH_URL" > /dev/null 2>&1; then
            HEALTHY=true
            break
        fi
        sleep 2
    done

    # Проверяем статус
    if [ "$HEALTHY" = true ]; then
        echo "  ✅ Engine API запущен и отвечает"
        curl -s "$HEALTH_URL" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"  📊 Статус: {d.get('status', 'unknown')}\")
print(f\"  📊 Сервис: {d.get('service', 'unknown')}\")
" 2>/dev/null || true
    else
        echo "  ❌ Engine API не ответил за ~60с"
        echo "  💡 Проверьте логи: screen -r rugpt-engine"
    fi
}

# Функция вывода информации
show_info() {
    echo ""
    echo "🎉 Перезапуск завершен!"
    echo ""
    echo "📋 Управление:"
    echo "  🔍 Логи Engine:        screen -r rugpt-engine"
    echo "  📱 Список экранов:     screen -ls"
    echo ""
    echo "🌐 Доступные эндпоинты:"
    echo "  📡 Engine API:         http://${CURL_HOST}:${API_PORT}"
    echo "  📖 API документация:   http://${CURL_HOST}:${API_PORT}/docs"
    echo "  💚 Health check:       ${HEALTH_URL}"
    echo ""
    echo "🔧 Конфигурация (из .env):"
    if [ -f "$SCRIPT_DIR/.env" ]; then
        DB_PORT=$(grep -E '^DB_PORT=' "$SCRIPT_DIR/.env" | cut -d= -f2)
        LLM_URL=$(grep -E '^LLM_BASE_URL=' "$SCRIPT_DIR/.env" | cut -d= -f2)
        MODEL=$(grep -E '^DEFAULT_MODEL=' "$SCRIPT_DIR/.env" | cut -d= -f2)
        echo "  * PostgreSQL:          rugpt (localhost:${DB_PORT:-5432})"
        echo "  * LLM:                 ${LLM_URL:-unknown}"
        echo "  * Модель по умолчанию: ${MODEL:-unknown}"
    else
        echo "  * .env не найден"
    fi
}

# Основная логика
case "$SERVICE" in
    "engine"|"")
        stop_engine
        start_engine
        show_info
        ;;
esac
