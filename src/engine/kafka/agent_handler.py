"""
Agent.requests consumer handler.

Called by KafkaConsumerLoop for each message on `agent.requests`. Pipeline:
 1. Parse payload + dispatch by `kind` (message_reply / poll_initial / poll_summary)
 2. Atomic CAS agent_runs status pending->running (idempotency)
 3. Branch:
    - message_reply: load user message, AIService.generate_response()
    - poll_initial:  AIService.generate_poll_initial(poll_id, chat_id, responder_id)
    - poll_summary:  AIService.generate_poll_summary(poll_id, chat_id, responder_id)
 4. Mark run as done/failed
 5. Publish the AI message to chat.events so NestJS broadcasts via WS

Designed to be robust to Kafka retries: if the same request_id is redelivered
after success, mark_running returns False and we skip silently.

Poison message protection: malformed payloads / unknown `kind` / missing
kind-specific required fields are logged and silently skipped (no raise),
so Kafka does not loop on garbage.
"""
from __future__ import annotations

from src.engine.unified_logger import get_logger
from typing import TYPE_CHECKING
from uuid import UUID

from ..config import Config

if TYPE_CHECKING:
    from ..services.ai_service import AIService
    from ..storage.message_storage import MessageStorage
    from ..storage.agent_run_storage import AgentRunStorage
    from .producer import KafkaProducerService

logger = get_logger("kafka")

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
        # --- Parse common fields ---
        try:
            request_id = UUID(payload["request_id"])
            chat_id = UUID(payload["chat_id"])
            responder_id = UUID(payload["responder_id"])
            kind = payload.get("kind", "message_reply")
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Malformed agent.requests payload: {e} payload={payload}")
            return  # don't raise — a bad payload can't be retried

        # --- Validate kind-specific fields BEFORE acquiring agent_run lock ---
        user_message_id: UUID | None = None
        strip_username = None
        poll_id: UUID | None = None

        if kind == "message_reply":
            user_message_id_raw = payload.get("user_message_id")
            if not user_message_id_raw:
                logger.error(
                    f"message_reply payload missing user_message_id: {payload}"
                )
                return
            try:
                user_message_id = UUID(user_message_id_raw)
            except (ValueError, TypeError):
                logger.error(
                    f"message_reply payload bad user_message_id: {payload}"
                )
                return
            strip_username = payload.get("strip_username")
        elif kind in ("poll_initial", "poll_summary"):
            poll_id_raw = payload.get("poll_id")
            if not poll_id_raw:
                logger.error(f"{kind} payload missing poll_id: {payload}")
                return
            try:
                poll_id = UUID(poll_id_raw)
            except (ValueError, TypeError):
                logger.error(f"{kind} payload bad poll_id: {payload}")
                return
        else:
            logger.error(f"Unknown kind={kind}, payload={payload}")
            return

        # --- Idempotency: atomic CAS pending -> running ---
        acquired = await self.agent_run_storage.mark_running(request_id)
        if not acquired:
            existing = await self.agent_run_storage.get(request_id)
            logger.info(
                f"agent_run {request_id} already status="
                f"{existing.status if existing else 'missing'}, skipping"
            )
            return

        try:
            # --- Dispatch ---
            if kind == "message_reply":
                user_message = await self.message_storage.get_by_id(user_message_id)
                if user_message is None:
                    raise RuntimeError(f"user_message {user_message_id} not found")
                ai_message = await self.ai_service.generate_response(
                    message=user_message,
                    responder_id=responder_id,
                    strip_username=strip_username,
                )
            elif kind == "poll_initial":
                ai_message = await self.ai_service.generate_poll_initial(
                    poll_id=poll_id,
                    chat_id=chat_id,
                    responder_id=responder_id,
                )
            elif kind == "poll_summary":
                ai_message = await self.ai_service.generate_poll_summary(
                    poll_id=poll_id,
                    chat_id=chat_id,
                    responder_id=responder_id,
                )
            else:
                # Unreachable — guarded above, but keep defensively.
                raise RuntimeError(f"unreachable kind={kind}")

            if ai_message is None:
                # ai_service уже залогировал WARNING с конкретной причиной
                # (нет роли / роль неактивна / mirror без sender'а / LLM None / итд)
                # — ищи `rugpt.services.ai` warning'и непосредственно перед этой строкой
                # по chat={chat_id} или responder=@username.
                await self.agent_run_storage.mark_failed(
                    request_id, f"{kind}: ai_service returned None — see ai_service warnings",
                )
                logger.warning(
                    f"agent_run {request_id} kind={kind} chat={chat_id} responder={responder_id}: "
                    f"ai_service returned None — see preceding rugpt.services.ai WARNINGs for cause"
                )
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
                f"agent_run {request_id} kind={kind} done: ai_message={ai_message.id} metadata={ai_message.metadata or {}}"
            )

        except Exception as e:
            logger.error(
                f"agent_run {request_id} kind={kind} failed: {e}", exc_info=True,
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
