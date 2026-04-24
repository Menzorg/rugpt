"""
AI Service

Service for generating AI responses.
Core rule: system user → their role responds (mirror → sender's role).
"""
from __future__ import annotations

import logging
import re
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from ..config import Config
from ..models.agent_run import AgentRun
from ..models.message import Message, Mention, MentionType, SenderType
from ..models.role import Role
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from ..storage.message_storage import MessageStorage
from ..storage.chat_storage import ChatStorage
from ..storage.agent_run_storage import AgentRunStorage
from .prompt_cache import PromptCache

# Avoid circular import: agents.executor -> services -> ai_service -> agents.executor
if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor
    from ..kafka.producer import KafkaProducerService

logger = logging.getLogger("rugpt.services.ai")


class AIService:
    """Service for AI response generation"""

    def __init__(
        self,
        role_storage: RoleStorage,
        user_storage: UserStorage,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        prompt_cache: Optional[PromptCache] = None,
        agent_executor: Optional[AgentExecutor] = None,
        agent_run_storage: Optional[AgentRunStorage] = None,
        kafka_producer: Optional["KafkaProducerService"] = None,
    ):
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.prompt_cache = prompt_cache
        self.agent_executor = agent_executor
        self.agent_run_storage = agent_run_storage
        self.kafka_producer = kafka_producer

    def _is_async_mode(self) -> bool:
        """Async path enabled iff both Kafka producer and agent_run storage are wired AND Kafka is enabled."""
        return (
            self.kafka_producer is not None
            and self.kafka_producer.enabled
            and self.agent_run_storage is not None
        )

    async def _enqueue_agent_run(
        self,
        message: Message,
        responder_id: UUID,
        strip_username: Optional[str] = None,
        role_code: str = "",
    ) -> Optional[UUID]:
        """Create AgentRun row + publish to agent.requests. Returns request_id or None on failure."""
        if not self._is_async_mode():
            return None
        request_id = uuid4()
        logger.info(
            f"Enqueue agent run: request_id={request_id} chat={message.chat_id} "
            f"responder={responder_id} role={role_code or '-'}"
        )
        run = AgentRun(
            request_id=request_id,
            chat_id=message.chat_id,
            user_message_id=message.id,
            triggering_user_id=message.sender_id,
            role_code=role_code or "",
            status="pending",
        )
        try:
            await self.agent_run_storage.create(run)
            await self.kafka_producer.send(
                Config.KAFKA_TOPIC_AGENT_REQUESTS,
                {
                    "request_id": str(request_id),
                    "chat_id": str(message.chat_id),
                    "user_message_id": str(message.id),
                    "triggering_user_id": str(message.sender_id),
                    "responder_id": str(responder_id),
                    "strip_username": strip_username,
                    "role_code": role_code or "",
                },
                key=str(message.chat_id),
            )
            return request_id
        except Exception as e:
            logger.error(f"Failed to enqueue agent run: {e}")
            return None

    async def try_auto_respond(
        self,
        message: Message,
        chat_id: UUID,
        sender_id: UUID,
    ) -> Optional[Message]:
        """
        Auto-respond if chat has a system user participant.
        Called when message has no @@ mentions.

        Async mode (Kafka enabled): publishes to agent.requests and returns None —
        the AI message will arrive later via chat.events WS broadcast. Returns None
        does NOT mean "no handler"; check try_auto_respond_async_enqueued if the
        caller needs to know whether a pending run was scheduled.

        Sync fallback: directly calls generate_response and returns the Message.
        """
        chat = await self.chat_storage.get_by_id(chat_id)
        if not chat:
            return None

        for pid in chat.participants:
            if pid == sender_id:
                continue
            participant = await self.user_storage.get_by_id(pid)
            if participant and participant.is_system:
                logger.info(
                    f"try_auto_respond: chat={chat_id} responder={participant.id} "
                    f"mode={'async' if self._is_async_mode() else 'sync'}"
                )
                if self._is_async_mode():
                    await self._enqueue_agent_run(
                        message=message,
                        responder_id=participant.id,
                    )
                    return None
                return await self.generate_response(
                    message=message,
                    responder_id=participant.id,
                )

        return None

    async def process_ai_mentions(
        self,
        message: Message,
        org_id: UUID
    ) -> List[Message]:
        """
        Process @@ mentions in a message and enqueue AI responses.

        Async mode (Kafka enabled): each mention -> AgentRun + Kafka publish;
        returns empty list. The caller should also consult `has_pending_agent_runs`
        to set `agent_pending=true` in the HTTP response.

        Sync fallback: returns list of generated AI messages inline.
        """
        ai_mentions = [m for m in message.mentions if m.type == MentionType.AI_ROLE]
        if not ai_mentions:
            return []

        mode = "async" if self._is_async_mode() else "sync"
        logger.info(
            f"process_ai_mentions: message={message.id} chat={message.chat_id} "
            f"mentions={len(ai_mentions)} mode={mode}"
        )

        if self._is_async_mode():
            for mention in ai_mentions:
                await self._enqueue_agent_run(
                    message=message,
                    responder_id=mention.user_id,
                    strip_username=mention.username,
                )
            return []

        responses = []
        for mention in ai_mentions:
            response = await self.generate_response(
                message=message,
                responder_id=mention.user_id,
                strip_username=mention.username,
            )
            if response:
                responses.append(response)

        return responses

    def has_pending_agent_runs(self, message: Message) -> bool:
        """Does this message trigger any agent work? Used for agent_pending HTTP flag.
        Cheap check — no storage hit, uses in-memory message attrs only."""
        return any(m.type == MentionType.AI_ROLE for m in (message.mentions or []))

    async def generate_response(
        self,
        message: Message,
        responder_id: UUID,
        strip_username: Optional[str] = None,
    ) -> Optional[Message]:
        """
        Generate AI response from a responder user.

        Core rule:
        - System user with role → respond using that role
        - System user without role (mirror) → respond using SENDER's role
        - Regular user with role → respond using that role

        Args:
            message: The user message to respond to
            responder_id: User ID of who should respond (system user or regular user)
            strip_username: If set, strip @@username from message content before sending to LLM
        """
        logger.info(
            f"generate_response: message={message.id} responder={responder_id}"
        )

        # Find responder
        responder = await self.user_storage.get_by_id(responder_id)
        if not responder:
            logger.warning(f"Responder {responder_id} not found")
            return None

        # Determine role
        role = await self._resolve_role(responder, message.sender_id)
        if not role:
            return None

        if not role.is_active:
            logger.warning(f"Role {role.id} is inactive")
            return None

        # Build conversation context
        conv_messages = await self._build_conversation(message, strip_username)

        # Generate
        try:
            response_content = await self._call_llm(role, conv_messages, user_id=message.sender_id, chat_id=message.chat_id)
            if response_content is None:
                return None

            ai_message = await self._create_ai_message(
                chat_id=message.chat_id,
                sender_id=responder_id,
                content=response_content,
                reply_to_id=message.id,
            )

            logger.info(
                f"AI response from {role.name} ({role.model_name}): "
                f"{len(response_content)} chars"
            )
            return ai_message

        except Exception as e:
            logger.error(f"Failed to generate AI response: {e}")
            return None

    async def _resolve_role(self, responder, sender_id: UUID) -> Optional[Role]:
        """
        Resolve which role to use for response.
        Mirror (is_system + no role) → sender's role.
        """
        if responder.role_id:
            role = await self.role_storage.get_by_id(responder.role_id)
            if not role:
                logger.warning(f"Role {responder.role_id} not found")
            return role

        if responder.is_system:
            # Mirror: use sender's role
            sender = await self.user_storage.get_by_id(sender_id)
            if not sender or not sender.role_id:
                logger.warning(f"Mirror: sender {sender_id} has no role")
                return None
            role = await self.role_storage.get_by_id(sender.role_id)
            if not role:
                logger.warning(f"Role {sender.role_id} not found")
            return role

        logger.warning(f"User {responder.id} has no role")
        return None

    async def _call_llm(self, role: Role, conv_messages: List[dict], user_id: Optional[UUID] = None, chat_id: Optional[UUID] = None) -> Optional[str]:
        """Call LLM via AgentExecutor."""
        if not self.agent_executor:
            logger.error("AIService has no agent_executor — cannot generate response")
            return None
        result = await self.agent_executor.execute(
            role=role,
            messages=conv_messages,
            temperature=0.7,
            max_tokens=256,
            user_id=user_id,
            chat_id=chat_id,
        )
        if result.finish_reason == "error":
            logger.error(f"Agent error: {result.error}")
            return None
        return result.content

    async def _build_conversation(
        self,
        message: Message,
        strip_username: Optional[str] = None,
    ) -> List[dict]:
        """Build conversation as list of {"role": str, "content": str} dicts."""
        messages = []

        # Recent chat history (last 10 messages)
        history = await self.message_storage.list_by_chat(message.chat_id, limit=10)

        for msg in history:
            if msg.id == message.id:
                continue
            role_name = "assistant" if msg.sender_type == SenderType.AI_ROLE else "user"
            messages.append({"role": role_name, "content": msg.content})

        # Current message
        content = message.content
        if strip_username:
            content = self._strip_mention(content, strip_username)
        messages.append({"role": "user", "content": content})

        return messages

    def _strip_mention(self, content: str, username: str) -> str:
        """Remove @@username from message content."""
        pattern = re.compile(rf'@@{re.escape(username)}\s*', re.IGNORECASE)
        cleaned = pattern.sub('', content).strip()
        return cleaned if cleaned else content

    async def _create_ai_message(
        self,
        chat_id: UUID,
        sender_id: UUID,
        content: str,
        reply_to_id: Optional[UUID] = None
    ) -> Message:
        """Create and save AI message"""
        from datetime import datetime
        from uuid import uuid4

        message = Message(
            id=uuid4(),
            chat_id=chat_id,
            sender_type=SenderType.AI_ROLE,
            sender_id=sender_id,
            content=content,
            mentions=[],
            reply_to_id=reply_to_id,
            ai_is_valid=None,
            ai_edited=False,
            is_deleted=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await self.message_storage.create(message)
        await self.chat_storage.update_last_message(chat_id)
        return created

    async def close(self):
        """No LLM client to close — inference goes through AgentExecutor which is owned by EngineService."""
        return None
