"""
Kafka consumer loop as a background asyncio task. Owned by EngineService
and started in EngineService.initialize().

Single topic per loop instance. Handler is called for each message; if it
raises, the offset is NOT committed and Kafka will redeliver the message.
Successful handle -> offset commit (at-least-once delivery).
"""
import asyncio
import json

from src.engine.unified_logger import get_logger
from typing import Awaitable, Callable, Optional

from ..config import Config
from ..logging_context import bind_correlation_id, correlation_id_var, user_id_var

logger = get_logger("kafka")

_CORRELATION_FIELD = "_correlation_id"
_USER_FIELD = "_user_id"

MessageHandler = Callable[[dict], Awaitable[None]]

class KafkaConsumerLoop:
    def __init__(
        self,
        topic: str,
        group_id: str,
        handler: MessageHandler,
        bootstrap_servers: Optional[str] = None,
    ):
        self.topic = topic
        self.group_id = group_id
        self.handler = handler
        self.bootstrap_servers = bootstrap_servers or Config.KAFKA_BOOTSTRAP_SERVERS
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"kafka-consumer-{self.topic}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        from aiokafka import AIOKafkaConsumer
        consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            enable_auto_commit=False,
            auto_offset_reset="latest",
        )
        await consumer.start()
        logger.info(f"Kafka consumer started: topic={self.topic} group={self.group_id}")
        try:
            while not self._stopping:
                batch = await consumer.getmany(timeout_ms=1000, max_records=10)
                for tp, messages in batch.items():
                    for msg in messages:
                        # Rebind correlation_id и user_id из payload, чтобы логи handler'а
                        # остались в той же трассе, что у HTTP-запроса, который publish'нул.
                        incoming_cid = (
                            msg.value.get(_CORRELATION_FIELD)
                            if isinstance(msg.value, dict)
                            else None
                        )
                        incoming_uid = (
                            msg.value.get(_USER_FIELD)
                            if isinstance(msg.value, dict)
                            else None
                        )
                        token_cid = bind_correlation_id(incoming_cid)
                        token_uid = user_id_var.set(incoming_uid)
                        try:
                            logger.info(
                                f"Kafka received: topic={self.topic} offset={msg.offset}"
                            )
                            await self.handler(msg.value)
                            await consumer.commit({tp: msg.offset + 1})
                        except Exception as e:
                            logger.error(
                                f"Handler failed for {self.topic} offset={msg.offset}: {e}",
                                exc_info=True,
                            )
                            # no commit -> redelivered
                        finally:
                            correlation_id_var.reset(token_cid)
                            user_id_var.reset(token_uid)
        except asyncio.CancelledError:
            pass
        finally:
            await consumer.stop()
            logger.info(f"Kafka consumer stopped: {self.topic}")
