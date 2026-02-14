#!/bin/bash
# -------------------------------------------
# local_restart.sh
# Robust restart script for RuGPT Engine
# Logs everything to file and keeps screen alive
# -------------------------------------------

SERVICE="$1"

case "$SERVICE" in
    "engine")
        echo "🔄 Restarting Engine..."
        ;;
    "")
        echo "🔄 Full RuGPT restart..."
        ;;
    *)
        echo "❌ Invalid parameter. Usage:"
        echo "  ./local_restart.sh              - full restart"
        echo "  ./local_restart.sh engine       - engine only"
        exit 1
        ;;
esac

# Bash-safe script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Log directory and file
mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/engine.log"

# ------------------------------
# Stop Engine
# ------------------------------
stop_engine() {
    echo "🛑 Stopping Engine..."

    # Stop screen session if exists
    screen -S rugpt-engine -X quit 2>/dev/null || true
    sleep 1

    # Kill Python / Uvicorn processes
    pkill -15 -f "uvicorn.*src.engine.app:app" || true
    pkill -15 -f "python.*src.engine.run" || true
    sleep 2

    # Force kill if still alive
    if pgrep -f "uvicorn.*src.engine.app:app\|python.*src.engine.run" > /dev/null; then
        echo "  ⚠️ Applying forced stop..."
        pkill -9 -f "uvicorn.*src.engine.app:app" || true
        pkill -9 -f "python.*src.engine.run" || true
        sleep 1
    fi

    # Free port 8100 in case of TIME_WAIT
    fuser -k 8100/tcp 2>/dev/null || true
    sleep 1

    echo "  ✅ Engine stopped"
}

# ------------------------------
# Start Engine
# ------------------------------
start_engine() {
    echo "🚀 Starting RuGPT Engine..."

    # Activate virtualenv and run migrations
    echo "🗂️ Running database migrations..."
    source .rugpt/bin/activate
    python -c "
import asyncio
from src.engine.migrations.migrate import run_migrations
asyncio.run(run_migrations())
print('✅ Migrations completed')
" 2>/dev/null || echo "⚠️ Migrations already done or failed"

    # Start Engine under screen, logging to file
    screen -dmS rugpt-engine -m bash -c "
cd '$SCRIPT_DIR' &&
source .rugpt/bin/activate &&
python -m src.engine.run 2>&1 | tee -a '$LOG_FILE';
exec bash
"

    echo "⏳ Waiting for Engine to start..."
    sleep 5

    # Health check
    if curl -s http://localhost:8100/api/v1/health > /dev/null 2>&1; then
        echo "  ✅ Engine API is running"
        curl -s http://localhost:8100/api/v1/health | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"  📊 Status: {d.get('status', 'unknown')}\")
print(f\"  📊 Service: {d.get('service', 'unknown')}\")
" 2>/dev/null || true
    else
        echo "  ❌ Engine API not responding"
        echo "  💡 Check logs: tail -f '$LOG_FILE'"
    fi
}

# ------------------------------
# Show info
# ------------------------------
show_info() {
    echo ""
    echo "🎉 Restart completed!"
    echo ""
    echo "📋 Management:"
    echo "  🔍 Engine logs:   tail -f '$LOG_FILE'"
    echo "  📱 Screen list:   screen -ls"
    echo ""
    echo "🌐 Endpoints:"
    echo "  📡 Engine API:         http://localhost:8100"
    echo "  📖 API docs:           http://localhost:8100/docs"
    echo "  💚 Health check:       http://localhost:8100/api/v1/health"
    echo ""
    echo "🔧 Configuration:"
    echo "  • PostgreSQL:          rugpt (localhost:5432)"
    echo "  • LLM:                 Ollama (localhost:11434)"
    echo "  • Default model:       qwen2:0.5b"
}

# ------------------------------
# Main
# ------------------------------
case "$SERVICE" in
    "engine"|"")
        stop_engine
        start_engine
        show_info
        ;;
esac
