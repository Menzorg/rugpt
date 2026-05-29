#!/bin/bash
# Start Kafka broker for RuGPT Engine (bitnami/kafka in KRaft mode).
# Idempotent: safe to run multiple times.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

echo "Starting Kafka broker..."
docker compose -f docker-compose.kafka.yml up -d

echo "Waiting for Kafka to be healthy..."
for i in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' rugpt-kafka 2>/dev/null || echo "unknown")
    if [ "$status" = "healthy" ]; then
        echo "Kafka is healthy"
        break
    fi
    sleep 2
done

if [ "$status" != "healthy" ]; then
    echo "Kafka did not become healthy in time. Current status: $status"
    docker compose -f docker-compose.kafka.yml logs --tail 50 kafka
    exit 1
fi

echo "Initializing topics..."
"$SCRIPT_DIR/kafka_init.sh"

echo "Done"
