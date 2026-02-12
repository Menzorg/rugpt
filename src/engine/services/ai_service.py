"""
AI Service

Service for generating AI responses to @@ mentions.
"""
from __future__ import annotations

import logging
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
            response = await self.generate_response_for_mention(
                message=message,
                mention=mention,
                org_id=org_id
            )
            if response:
                responses.append(response)

        return responses

    async def generate_response_for_mention(
        self,
        message: Message,
        mention: Mention,
        org_id: UUID
    ) -> Optional[Message]:
        """
        Generate AI response for a specific @@ mention.

        1. Find the mentioned user
        2. Get their assigned role
        3. Generate response using role's system prompt and model
        4. Save and return the AI message
        """
        # Find mentioned user
        user = await self.user_storage.get_by_id(mention.user_id)
        if not user:
            logger.warning(f"User {mention.user_id} not found for AI mention")
            return None

        # Get user's assigned role
        if not user.role_id:
            logger.warning(f"User {mention.user_id} has no assigned role")
            return None

        role = await self.role_storage.get_by_id(user.role_id)
        if not role:
            logger.warning(f"Role {user.role_id} not found")
            return None

        if not role.is_active:
            logger.warning(f"Role {role.id} is inactive")
            return None

        # Build conversation context as dicts for AgentExecutor
        conv_messages = await self._build_conversation_messages(
            message=message,
            mention=mention
        )

        # Generate response via AgentExecutor (or fallback to old OllamaProvider)
        try:
            if self.agent_executor:
                agent_result = await self.agent_executor.execute(
                    role=role,
                    messages=conv_messages,
                    temperature=0.7,
                    max_tokens=256,
                )

                if agent_result.finish_reason == "error":
                    logger.error(f"Agent error: {agent_result.error}")
                    return None

                response_content = agent_result.content
            else:
                # Fallback: old OllamaProvider path
                llm_messages = self._dicts_to_llm_messages(conv_messages, role)
                llm_response = await self.llm.generate(
                    messages=llm_messages,
                    model=role.model_name,
                    temperature=0.7,
                    max_tokens=256,
                )
                if llm_response.finish_reason == "error":
                    logger.error(f"LLM error: {llm_response.content}")
                    return None
                response_content = llm_response.content

            # Create AI message (unvalidated)
            ai_message = await self._create_ai_message(
                chat_id=message.chat_id,
                sender_id=mention.user_id,
                content=response_content,
                reply_to_id=message.id
            )

            logger.info(
                f"Generated AI response from {role.name} ({role.model_name}): "
                f"{len(response_content)} chars"
            )

            return ai_message

        except Exception as e:
            logger.error(f"Failed to generate AI response: {e}")
            return None

    async def _build_conversation_messages(
        self,
        message: Message,
        mention: Mention,
    ) -> List[dict]:
        """Build conversation as list of {"role": str, "content": str} dicts.
        Used by AgentExecutor (system prompt is handled by executor via PromptCache).
        """
        messages = []

        # Get recent chat history for context (last 10 messages)
        history = await self.message_storage.list_by_chat(
            message.chat_id,
            limit=10
        )

        # Add history (oldest first)
        for msg in history:
            if msg.id == message.id:
                continue
            role_name = "assistant" if msg.sender_type == SenderType.AI_ROLE else "user"
            messages.append({"role": role_name, "content": msg.content})

        # Add current message (extract content after @@ mention)
        user_content = self._extract_content_for_ai(message.content, mention.username)
        messages.append({"role": "user", "content": user_content})

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

    def _extract_content_for_ai(self, content: str, username: str) -> str:
        """Extract the message content for AI (remove @@ mention prefix)"""
        import re
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
            ai_validated=False,  # Requires user validation
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
