"""
Agent.requests consumer handler.

Called by KafkaConsumerLoop for each message on `agent.requests`. Pipeline:
 1. Atomic CAS agent_runs status pending->running (idempotency)
 2. Load the user message that triggered this run
 3. Call AIService.generate_response() — the existing synchronous logic
    that builds context, invokes LangGraph agent, persists AI message
 4. Mark run as done/failed
 5. Publish the AI message to chat.events so NestJS broadcasts via WS

Designed to be robust to Kafka retries: if the same request_id is redelivered
after success, mark_running returns False and we skip silently.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from ..config import Config

if TYPE_CHECKING:
    from ..services.ai_service import AIService
    from ..storage.message_storage import MessageStorage
    from ..storage.agent_run_storage import AgentRunStorage
    from .producer import KafkaProducerService

logger = logging.getLogger("rugpt.kafka.agent_handler")


class AgentRequestHandler:
    def __init__(
        self,
        ai_service: "AIService",
        message_storage: "MessageStorage",
        agent_run_storage: "AgentRunStorage",
        kafka_producer: "KafkaProducerService",
    ):
        self.ai_service = ai_service
        self.message_storage = message_storage
        self.agent_run_storage = agent_run_storage
        self.kafka_producer = kafka_producer

    async def __call__(self, payload: dict) -> None:
        """Entry point for KafkaConsumerLoop. Raises on failure to trigger retry."""
        try:
            request_id = UUID(payload["request_id"])
            chat_id = UUID(payload["chat_id"])
            user_message_id = UUID(payload["user_message_id"])
            responder_id = UUID(payload["responder_id"])
            strip_username = payload.get("strip_username")
        except (KeyError, ValueError) as e:
            logger.error(f"Malformed agent.requests payload: {e} payload={payload}")
            return  # don't raise — a bad payload can't be retried

        # Idempotency: atomic CAS pending -> running
        acquired = await self.agent_run_storage.mark_running(request_id)
        if not acquired:
            existing = await self.agent_run_storage.get(request_id)
            logger.info(
                f"agent_run {request_id} already status={existing.status if existing else 'missing'}, skipping"
            )
            return

        try:
            # Load the triggering user message
            user_message = await self.message_storage.get_by_id(user_message_id)
            if user_message is None:
                raise RuntimeError(f"user_message {user_message_id} not found")

            # Call existing synchronous generation pipeline — it handles
            # role resolution, context build, LangGraph run, persistence.
            ai_message = await self.ai_service.generate_response(
                message=user_message,
                responder_id=responder_id,
                strip_username=strip_username,
            )

            if ai_message is None:
                await self.agent_run_storage.mark_failed(
                    request_id, "generate_response returned None",
                )
                logger.warning(f"agent_run {request_id}: generate_response returned None")
                return

            await self.agent_run_storage.mark_done(request_id, ai_message.id)

            # Publish to chat.events for live WS delivery
            try:
                await self.kafka_producer.send(
                    Config.KAFKA_TOPIC_CHAT_EVENTS,
                    {
                        "chat_id": str(chat_id),
                        "message": ai_message.to_dict(),
                    },
                    key=str(chat_id),
                )
            except Exception as e:
                logger.error(f"Failed to publish AI message to chat.events: {e}")

            logger.info(
                f"agent_run {request_id} done: ai_message={ai_message.id}"
            )

        except Exception as e:
            logger.error(
                f"agent_run {request_id} failed: {e}", exc_info=True,
            )
            try:
                await self.agent_run_storage.mark_failed(request_id, str(e))
            except Exception as mark_err:
                logger.error(f"Failed to mark_failed: {mark_err}")
            # Re-raise so KafkaConsumerLoop does NOT commit offset — Kafka will retry.
            # Note: mark_failed already ran, so on retry mark_running will fail
            # (status is 'failed', not 'pending'), and we'll skip. This is OK:
            # one retry attempt per Kafka redelivery cycle, then eventually commit
            # happens through the "already failed, skip silently" path.
            # For Alpha this is acceptable.
            raise
