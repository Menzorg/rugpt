#!/bin/bash
# Перезапуск RuGPT Engine

# Загружаем .env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

# Defaults (если .env не задал значения)
API_HOST="${API_HOST:-localhost}"
API_PORT="${API_PORT:-8100}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-rugpt}"
DEFAULT_MODEL="${DEFAULT_MODEL:-qwen2.5:7b}"

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
cd "$SCRIPT_DIR"

# Функция остановки Engine
stop_engine() {
    echo "🛑 Останавливаем Engine..."

    # Останавливаем screen
    screen -S rugpt-engine -X quit 2>/dev/null || true
    sleep 2

    # Останавливаем uvicorn процессы
    echo "🛑 Останавливаем uvicorn процессы..."
    pkill -15 -f "uvicorn.*src.engine.app:app" || true
    pkill -15 -f "python.*src.engine.run" || true
    sleep 2

    # Проверяем, остались ли процессы
    if pgrep -f "uvicorn.*src.engine.app:app\|python.*src.engine.run" > /dev/null; then
        echo "  ⚠️ Применяем принудительную остановку..."
        pkill -9 -f "uvicorn.*src.engine.app:app" || true
        pkill -9 -f "python.*src.engine.run" || true
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

    # Ждем запуска
    echo "⏳ Ожидаем запуска Engine..."
    sleep 5

    # Проверяем статус
    if curl -s "http://${API_HOST}:${API_PORT}/api/v1/health" > /dev/null 2>&1; then
        echo "  ✅ Engine API запущен и отвечает"
        curl -s "http://${API_HOST}:${API_PORT}/api/v1/health" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"  📊 Статус: {d.get('status', 'unknown')}\")
print(f\"  📊 Сервис: {d.get('service', 'unknown')}\")
" 2>/dev/null || true
    else
        echo "  ❌ Engine API не отвечает"
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
    echo "  📡 Engine API:         http://${API_HOST}:${API_PORT}"
    echo "  📖 API документация:   http://${API_HOST}:${API_PORT}/docs"
    echo "  💚 Health check:       http://${API_HOST}:${API_PORT}/api/v1/health"
    echo ""
    echo "🔧 Конфигурация:"
    echo "  • PostgreSQL:          ${DB_NAME} (${DB_HOST}:${DB_PORT})"
    echo "  • LLM:                 Ollama"
    echo "  • Модель по умолчанию: ${DEFAULT_MODEL}"
}

# Основная логика
case "$SERVICE" in
    "engine"|"")
        stop_engine
        start_engine
        show_info
        ;;
esac
