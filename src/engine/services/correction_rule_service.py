"""
Correction Rule Service

Creates correction rules when an admin rejects an AI response, and drives
async extraction of the lesson from the correction text via LLM.
"""
from __future__ import annotations

import logging
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from langchain_openai import OpenAIEmbeddings

from ..models.correction_rule import CorrectionRule
from ..models.message import SenderType
from ..storage.correction_rule_storage import CorrectionRuleStorage
from ..storage.message_storage import MessageStorage
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from .chat_service import ChatService

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor

logger = logging.getLogger("rugpt.services.correction_rule")

LESSON_EXTRACTION_SYSTEM_PROMPT = (
                "Ты — ассистент, извлекающий полезные уроки из исправлений AI-ответов.\n"
                "На основе вопроса пользователя, ответа AI и исправления — сформулируй "
                "краткий урок для улучшения будущих ответов.\n"
                "Структурируй урок в следующем формате: \n\n"
                "Ситуация: ...,\n"
                "Плохой пример: ...,\n"
                "Правильный пример: ...,\n"
                "Краткое объяснение: ...,"
            )

class CorrectionRuleService:
    """Service for correction rules lifecycle"""

    def __init__(
        self,
        correction_rule_storage: CorrectionRuleStorage,
        message_storage: MessageStorage,
        role_storage: RoleStorage,
        user_storage: UserStorage,
        chat_service: ChatService,
        agent_executor: Optional[AgentExecutor] = None,
        embedding_model: str = "",
        llm_base_url: str = "",
        llm_api_key: str = "",
        kafka_producer=None,
    ):
        self.correction_rule_storage = correction_rule_storage
        self.message_storage = message_storage
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.chat_service = chat_service
        self.agent_executor = agent_executor
        self.kafka_producer = kafka_producer
        self._embeddings = OpenAIEmbeddings(
            model=embedding_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )

    async def reject_and_create_rule(
        self,
        ai_message_id: UUID,
        user_id: UUID,
        correction_text: str,
    ) -> CorrectionRule:
        """
        Reject an AI message and create a correction rule.

        Resolves the role from the AI sender, links the source user message
        and AI response, then fires async lesson extraction.

        Args:
            ai_message_id: The AI message being rejected (src_ai_response_id).
            user_id:       The admin or role-owner performing the rejection.
            correction_text: What was wrong / what the correct answer should be.
        """
        ai_message = await self.message_storage.get_by_id(ai_message_id)
        if not ai_message:
            raise ValueError(f"AI message {ai_message_id} not found")
        if ai_message.sender_type != SenderType.AI_ROLE:
            raise ValueError(f"Message {ai_message_id} is not an AI message")

        original_message = None
        if ai_message.reply_to_id:
            original_message = await self.message_storage.get_by_id(ai_message.reply_to_id)

        rejecter = await self.user_storage.get_by_id(user_id)
        if not rejecter:
            raise ValueError(f"User {user_id} not found")

        ai_sender = await self.user_storage.get_by_id(ai_message.sender_id)
        is_mirror_response = (
            ai_sender is not None
            and ai_sender.is_system
            and ai_sender.role_id is None
        )

        # Resolve role_id: AI sender's role → mirror trigger's role → rejecter's role
        if ai_sender is not None and ai_sender.role_id is not None:
            role_id = ai_sender.role_id
        elif is_mirror_response and original_message is not None:
            triggering_user = await self.user_storage.get_by_id(original_message.sender_id)
            if triggering_user is None or triggering_user.role_id is None:
                raise ValueError(
                    f"Cannot determine role: mirror trigger {original_message.sender_id} has no role"
                )
            role_id = triggering_user.role_id
        elif rejecter.role_id is not None:
            role_id = rejecter.role_id
        else:
            raise ValueError(
                f"Cannot determine role for correction (rejecter {user_id} has no role)"
            )

        await self.message_storage.reject(ai_message_id)


        lesson = await self._extract_lesson(
            src_ai_response_id=ai_message_id,
            src_user_message_id=original_message.id if original_message else None,
            user_correction_text=correction_text,
        )
        
        rule = CorrectionRule(
            id=uuid4(),
            role_id=role_id,
            src_user_message_id=original_message.id if original_message else None,
            src_ai_response_id=ai_message_id,
            user_correction_text=correction_text,
            extracted_lesson=lesson,
        )
        
        # Эхо коррекции в чат — это комментарий ЧЕЛОВЕКА (владельца роли),
        # а не нового ответа AI. Использовать SenderType.USER чтобы:
        # (1) пузырь рендерился как обычное сообщение от человека, не как AI,
        # (2) ai_is_valid автоматически True (см. chat_service.send_message),
        #     иначе эхо попадает в pending-review и засоряет список валидаций.
        echo = await self.chat_service.send_message(
            chat_id=ai_message.chat_id,
            sender_id=user_id,
            content=correction_text,
            sender_type=SenderType.USER,
            reply_to_id=ai_message_id,
        )

        # Публикация в chat.events — иначе участники чата не увидят эхо
        # без перезагрузки страницы (chat_service.send_message сам в Kafka
        # не пишет, broadcast делает webclient kafka-consumer).
        if self.kafka_producer is not None:
            try:
                from ..config import Config
                await self.kafka_producer.send(
                    Config.KAFKA_TOPIC_CHAT_EVENTS,
                    {"chat_id": str(echo.chat_id), "message": echo.to_dict()},
                    key=str(echo.chat_id),
                )
            except Exception as e:
                logger.error(f"Failed to publish correction echo to Kafka: {e}")
        
        created_rule = await self.correction_rule_storage.create(rule)
        logger.info("correction rule %s created for role %s", created_rule.id, role_id)
        return created_rule

    async def update_rule(
        self,
        rule_id: UUID,
        src_ai_response_id: Optional[UUID] = None,
        user_correction_text: Optional[str] = None,
    ) -> Optional[CorrectionRule]:
        """
        Update admin-editable fields and re-extract the lesson if anything changed.
        """
        rule = await self.correction_rule_storage.get_by_id(rule_id)
        if not rule:
            return None

        changed = False
        if src_ai_response_id is not None and src_ai_response_id != rule.src_ai_response_id:
            rule.src_ai_response_id = src_ai_response_id
            changed = True
        if user_correction_text is not None and user_correction_text != rule.user_correction_text:
            rule.user_correction_text = user_correction_text
            changed = True

        if not changed:
            return rule

        lesson = await self._extract_lesson(
            src_ai_response_id=rule.src_ai_response_id,
            src_user_message_id=rule.src_user_message_id,
            user_correction_text=rule.user_correction_text,
        )
        rule.extracted_lesson = lesson
        return await self.correction_rule_storage.update(rule)

    async def _extract_lesson(
        self,
        src_ai_response_id: UUID,
        src_user_message_id: UUID,
        user_correction_text: str,
    ) -> Optional[str]:
        """Call LLM to extract a lesson string from the correction context. Returns None on failure."""
        if not self.agent_executor:
            logger.warning("no agent_executor — skipping lesson extraction")
            return None

        try:
            ai_message = None
            if src_ai_response_id:
                ai_message = await self.message_storage.get_by_id(src_ai_response_id)
            else:
                logger.error("_extract_lesson: src_ai_response_id is None")

            user_message = None
            if src_user_message_id:
                user_message = await self.message_storage.get_by_id(src_user_message_id)
            else:
                logger.error("_extract_lesson: src_user_message_id is None")

            llm = self.agent_executor._create_llm(
                model=self.agent_executor.default_model, temperature=0.7
            )

            # Formation of user prompt from user message, AI response and correction
            user_parts = []
            if user_message:
                user_parts.append(f"Вопрос пользователя:\n{user_message.content}")
            if ai_message:
                user_parts.append(f"Ответ AI:\n{ai_message.content}")
            user_parts.append(f"Исправление:\n{user_correction_text}")
            user_prompt = "\n\n".join(user_parts)

            result = await llm.ainvoke([
                {"role": "system", "content": LESSON_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ])
            lesson = result.content.strip()
            return lesson if lesson else None

        except Exception:
            logger.exception("lesson extraction failed")
            return None

    async def search_corrections(
        self,
        user_prompt: str,
        memory_text: str,
        top_k: int = 3,
    ) -> List[CorrectionRule]:
        """Search correction rules by semantic similarity to a user prompt and memory string."""
        user_embedding = await self._embeddings.aembed_query(user_prompt)
        mem_embedding = await self._embeddings.aembed_query(memory_text)
        return await self.correction_rule_storage.search_by_embeddings(
            mem_embedding=mem_embedding,
            user_message_embedding=user_embedding,
            top_k=top_k,
        )

    async def get_rules_for_role(self, role_id: UUID, active_only: bool = True) -> List[CorrectionRule]:
        """Get correction rules for a role."""
        return await self.correction_rule_storage.list_by_role(role_id, active_only=active_only)

    async def get_rule(self, rule_id: UUID) -> Optional[CorrectionRule]:
        """Get a correction rule by ID."""
        return await self.correction_rule_storage.get_by_id(rule_id)

    async def deactivate_rule(self, rule_id: UUID) -> bool:
        """Soft-delete a correction rule."""
        return await self.correction_rule_storage.deactivate(rule_id)
