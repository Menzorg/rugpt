"""
Kafka producer service (item 10 / async agent execution).

Wraps aiokafka AIOKafkaProducer with JSON serialization and idempotent
producer semantics (exactly-once per partition). Single instance lives
on EngineService.

If Config.KAFKA_ENABLED is false, send() becomes a no-op so tests and
environments without Kafka keep working.
"""
import json
import logging
from typing import Any, Optional
from uuid import UUID

from ..config import Config
from ..logging_context import get_correlation_id

logger = logging.getLogger("rugpt.kafka.producer")

_CORRELATION_FIELD = "_correlation_id"


def _json_default(o: Any) -> Any:
    """JSON default encoder — handles UUID and datetime-like objects."""
    if isinstance(o, UUID):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class KafkaProducerService:
    def __init__(self, bootstrap_servers: Optional[str] = None, enabled: Optional[bool] = None):
        self.bootstrap_servers = bootstrap_servers or Config.KAFKA_BOOTSTRAP_SERVERS
        self.enabled = Config.KAFKA_ENABLED if enabled is None else enabled
        self._producer = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Kafka producer disabled (Config.KAFKA_ENABLED=false)")
            return
        if self._producer is not None:
            return
        # Lazy import so test envs without aiokafka don't fail on import.
        from aiokafka import AIOKafkaProducer
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v, default=_json_default).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if isinstance(k, str) else k,
            enable_idempotence=True,
            acks="all",
        )
        await self._producer.start()
        logger.info(f"Kafka producer started: {self.bootstrap_servers}")

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("Kafka producer stopped")

    async def send(self, topic: str, value: dict, key: Optional[str] = None) -> None:
        """Send a JSON-serializable value to a topic. No-op when disabled.

        Injects the current correlation_id into the payload under
        `_correlation_id` so the downstream consumer can rebind it and keep
        the trace coherent across the async boundary.
        """
        if not self.enabled or self._producer is None:
            return
        if _CORRELATION_FIELD not in value:
            value = {**value, _CORRELATION_FIELD: get_correlation_id()}
        logger.info(f"Kafka send: topic={topic} key={key}")
        await self._producer.send_and_wait(topic, value=value, key=key)
