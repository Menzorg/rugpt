#!/bin/bash
# Create Kafka topics used by RuGPT Engine + webclient.
# Idempotent: uses --if-not-exists so re-runs are safe.
set -e

KAFKA_CONTAINER="${KAFKA_CONTAINER:-rugpt-kafka}"
BOOTSTRAP="${KAFKA_BOOTSTRAP_SERVERS:-localhost:9092}"

run_topic_cmd() {
    docker exec "$KAFKA_CONTAINER" /opt/kafka/bin/kafka-topics.sh --bootstrap-server "$BOOTSTRAP" "$@"
}

echo "Creating topic: agent.requests (3 partitions, 24h retention)"
run_topic_cmd --create --if-not-exists \
    --topic agent.requests \
    --partitions 3 \
    --replication-factor 1 \
    --config retention.ms=86400000

echo "Creating topic: chat.events (3 partitions, 1h retention)"
run_topic_cmd --create --if-not-exists \
    --topic chat.events \
    --partitions 3 \
    --replication-factor 1 \
    --config retention.ms=3600000

echo "Current topics:"
run_topic_cmd --list
