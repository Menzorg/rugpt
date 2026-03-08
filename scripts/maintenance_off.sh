#!/bin/bash
#
# Выключает режим технического обслуживания на webclient
# Использование: ./maintenance_off.sh
#

WEBCLIENT_URL="http://10.0.0.1/api/config/maintenance"

echo "🔧 Выключаю режим технического обслуживания..."

echo "  🌐 Webclient..."
RESPONSE=$(curl -sf -X POST "$WEBCLIENT_URL" \
    -H "Content-Type: application/json" \
    -d '{"maintenance": false}' 2>&1)

if echo "$RESPONSE" | grep -q '"success":true'; then
    echo "     ✅ Webclient: maintenance выключен (клиенты уведомлены через WebSocket)"
else
    echo "     ❌ Webclient: ошибка - $RESPONSE"
fi

echo ""
echo "✅ Режим технического обслуживания выключен"
