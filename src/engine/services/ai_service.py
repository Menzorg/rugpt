"""
AI Service

Service for generating AI responses.
Core rule: system user → their role responds (mirror → sender's role).
"""
from __future__ import annotations

import logging
import re
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID

from ..models.message import Message, Mention, MentionType, SenderType
from ..models.role import Role
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from ..storage.message_storage import MessageStorage
from ..storage.chat_storage import ChatStorage
from ..llm.providers.ollama import OllamaProvider
from ..llm.providers.base import LLMMessage
from .prompt_cache import PromptCache

# Avoid circular import: agents.executor -> services -> ai_service -> agents.executor
if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor

logger = logging.getLogger("rugpt.services.ai")


class AIService:
    """Service for AI response generation"""

    def __init__(
        self,
        role_storage: RoleStorage,
        user_storage: UserStorage,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        llm_provider: Optional[OllamaProvider] = None,
        prompt_cache: Optional[PromptCache] = None,
        agent_executor: Optional[AgentExecutor] = None,
    ):
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.llm = llm_provider or OllamaProvider()
        self.prompt_cache = prompt_cache
        self.agent_executor = agent_executor

    async def try_auto_respond(
        self,
        message: Message,
        chat_id: UUID,
        sender_id: UUID,
    ) -> Optional[Message]:
        """
        Auto-respond if chat has a system user participant.
        Called when message has no @@ mentions.

        Finds system user among chat participants → generate_response().
        """
        chat = await self.chat_storage.get_by_id(chat_id)
        if not chat:
            return None

        for pid in chat.participants:
            if pid == sender_id:
                continue
            participant = await self.user_storage.get_by_id(pid)
            if participant and participant.is_system:
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
        Process @@ mentions in a message and generate AI responses.
        Returns list of AI response messages.
        """
        ai_mentions = [m for m in message.mentions if m.type == MentionType.AI_ROLE]
        if not ai_mentions:
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
            response_content = await self._call_llm(role, conv_messages)
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

    async def _call_llm(self, role: Role, conv_messages: List[dict]) -> Optional[str]:
        """Call LLM via AgentExecutor or fallback to OllamaProvider."""
        if self.agent_executor:
            result = await self.agent_executor.execute(
                role=role,
                messages=conv_messages,
                temperature=0.7,
                max_tokens=256,
            )
            if result.finish_reason == "error":
                logger.error(f"Agent error: {result.error}")
                return None
            return result.content

        # Fallback: old OllamaProvider
        llm_messages = self._dicts_to_llm_messages(conv_messages, role)
        response = await self.llm.generate(
            messages=llm_messages,
            model=role.model_name,
            temperature=0.7,
            max_tokens=256,
        )
        if response.finish_reason == "error":
            logger.error(f"LLM error: {response.content}")
            return None
        return response.content

    async def _build_conversation(
        self,
        message: Message,
        strip_username: Optional[str] = None,
    ) -> List[dict]:
        """Build conversation as list of {"role": str, "content": str} dicts."""
        messages = []

        # Recent chat history (last 10 messages)
        history = await self.message_storage.list_by_chat(message.chat_id, limit=10)

        for msg in reversed(history):
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

    def _dicts_to_llm_messages(self, conv_messages: List[dict], role: Role) -> List[LLMMessage]:
        """Convert dict messages to LLMMessage objects (fallback for old OllamaProvider)"""
        llm_messages = []
        system_prompt = (
            self.prompt_cache.get_prompt(role) if self.prompt_cache
            else (role.system_prompt or "")
        )
        if system_prompt:
            llm_messages.append(LLMMessage(role="system", content=system_prompt))
        for msg in conv_messages:
            llm_messages.append(LLMMessage(role=msg["role"], content=msg["content"]))
        return llm_messages

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
            ai_validated=False,
            ai_edited=False,
            is_deleted=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        created = await self.message_storage.create(message)
        await self.chat_storage.update_last_message(chat_id)
        return created

    async def check_llm_health(self) -> bool:
        """Check if LLM is available"""
        return await self.llm.health_check()

    async def list_available_models(self) -> List[str]:
        """List available LLM models"""
        return await self.llm.list_models()

    async def close(self):
        """Close LLM client"""
        await self.llm.close()
