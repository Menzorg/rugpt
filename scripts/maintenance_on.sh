#!/bin/bash
#
# Включает режим технического обслуживания на webclient
# Использование: ./maintenance_on.sh [сообщение]
#

MESSAGE="${1:-Техническое обслуживание. Пожалуйста, подождите...}"
WEBCLIENT_URL="http://10.0.0.1/api/config/maintenance"

echo "🔧 Включаю режим технического обслуживания..."

echo "  🌐 Webclient..."
RESPONSE=$(curl -sf -X POST "$WEBCLIENT_URL" \
    -H "Content-Type: application/json" \
    -d "{\"maintenance\": true, \"message\": \"$MESSAGE\"}" 2>&1)

if echo "$RESPONSE" | grep -q '"success":true'; then
    echo "     ✅ Webclient: maintenance включён (клиенты уведомлены через WebSocket)"
else
    echo "     ❌ Webclient: ошибка - $RESPONSE"
fi

echo ""
echo "✅ Режим технического обслуживания включён"
echo "   Сообщение: $MESSAGE"
