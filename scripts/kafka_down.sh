#!/bin/bash
# Stop Kafka broker (preserves data volume).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

docker compose -f docker-compose.kafka.yml down

echo "Kafka stopped. Data volume rugpt-kafka-data is preserved."
echo "To wipe data: docker volume rm rugpt-kafka-data"
