"""
Correction Rule Service

Handles rejection of AI responses, creation of correction rules,
and generation of rule_text via LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from ..models.correction_rule import CorrectionRule
from ..models.message import Message, SenderType
from ..storage.correction_rule_storage import CorrectionRuleStorage
from ..storage.message_storage import MessageStorage
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from .chat_service import ChatService

if TYPE_CHECKING:
    from ..agents.executor import AgentExecutor

logger = logging.getLogger("rugpt.services.correction_rule")


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
    ):
        self.correction_rule_storage = correction_rule_storage
        self.message_storage = message_storage
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.chat_service = chat_service
        self.agent_executor = agent_executor

    async def reject_and_create_rule(
        self,
        ai_message_id: UUID,
        user_id: UUID,
        correction_text: str,
    ) -> CorrectionRule:
        """
        Reject AI message, send correction comment to chat, create rule.

        Args:
            ai_message_id: The AI message being rejected
            user_id: The user rejecting (role owner)
            correction_text: What was wrong / correction

        Returns:
            Created CorrectionRule
        """
        # 1. Get AI message
        ai_message = await self.message_storage.get_by_id(ai_message_id)
        if not ai_message:
            raise ValueError(f"AI message {ai_message_id} not found")

        if ai_message.sender_type != SenderType.AI_ROLE:
            raise ValueError(f"Message {ai_message_id} is not an AI message")

        # 2. Get the original user question (reply_to_id)
        original_message = None
        if ai_message.reply_to_id:
            original_message = await self.message_storage.get_by_id(ai_message.reply_to_id)

        user_question = original_message.content if original_message else ""

        # Authorization: admin bypass, or rejecter must be the role-owner,
        # or — for mirror responses (is_system + no role_id) — the user who
        # triggered the reply.
        rejecter = await self.user_storage.get_by_id(user_id)
        if not rejecter:
            raise ValueError(f"User {user_id} not found")

        ai_sender = await self.user_storage.get_by_id(ai_message.sender_id)
        is_mirror_response = (
            ai_sender is not None
            and ai_sender.is_system
            and ai_sender.role_id is None
        )

        if not rejecter.is_admin:
            if ai_message.sender_id != user_id:
                if not is_mirror_response:
                    raise ValueError(
                        f"User {user_id} is not the owner of this AI role response"
                    )
                if not original_message or original_message.sender_id != user_id:
                    raise ValueError(
                        f"User {user_id} did not trigger this mirror response"
                    )

        # 3. Determine effective role_id / org_id for the CorrectionRule.
        # Priority:
        #   a) AI sender has its own role (domain or system AI) — the rule
        #      belongs to that role
        #   b) Mirror response — the rule belongs to the triggering user's role
        #   c) Fallback — rejecter's own role (legacy path, same as role-owner
        #      rejecting their own response)
        if ai_sender is not None and ai_sender.role_id is not None:
            role_id = ai_sender.role_id
            org_id = ai_sender.org_id
        elif is_mirror_response and original_message is not None:
            triggering_user = await self.user_storage.get_by_id(original_message.sender_id)
            if triggering_user is None or triggering_user.role_id is None:
                raise ValueError(
                    f"Cannot determine role: mirror trigger {original_message.sender_id} has no role"
                )
            role_id = triggering_user.role_id
            org_id = triggering_user.org_id
        elif rejecter.role_id is not None:
            role_id = rejecter.role_id
            org_id = rejecter.org_id
        else:
            raise ValueError(
                f"Cannot determine role for correction (rejecter {user_id} has no role)"
            )

        # 4. Reject the AI message (set ai_is_valid = false)
        await self.message_storage.reject(ai_message_id)

        # 5. Send correction comment to the same chat from the user
        await self.chat_service.send_message(
            chat_id=ai_message.chat_id,
            sender_id=user_id,
            content=correction_text,
            sender_type=SenderType.USER,
            reply_to_id=ai_message_id,
        )

        # 6. Create correction rule
        rule = CorrectionRule(
            id=uuid4(),
            role_id=role_id,
            org_id=org_id,
            original_message_id=original_message.id if original_message else ai_message_id,
            ai_message_id=ai_message_id,
            chat_id=ai_message.chat_id,
            user_question=user_question,
            ai_answer=ai_message.content,
            correction_text=correction_text,
            rule_text=None,  # Generated async by LLM
            created_by_user_id=user_id,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        created_rule = await self.correction_rule_storage.create(rule)
        logger.info(f"Correction rule {created_rule.id} created for role {role_id}")

        # 7. Generate rule_text via LLM (fire-and-forget style, but awaited)
        await self._generate_rule_text(created_rule)

        return created_rule

    async def _generate_rule_text(self, rule: CorrectionRule) -> None:
        """Generate rule_text using LLM via AgentExecutor"""
        if not self.agent_executor:
            logger.warning("No agent_executor, skipping rule_text generation")
            return

        try:
            from ..agents.graphs.rule_generator import generate_rule_text
            from ..config import Config

            rule_text = await generate_rule_text(
                base_url=self.agent_executor.base_url,
                model=self.agent_executor.default_model,
                user_question=rule.user_question,
                ai_answer=rule.ai_answer,
                correction_text=rule.correction_text,
            )

            if rule_text:
                await self.correction_rule_storage.update_rule_text(rule.id, rule_text)
                logger.info(f"Rule text generated for rule {rule.id}")

        except Exception as e:
            logger.error(f"Failed to generate rule_text for rule {rule.id}: {e}")

    async def get_rules_for_role(self, role_id: UUID) -> List[CorrectionRule]:
        """
        Get relevant correction rules for a role.

        TODO: Replace with RAG-based semantic search.
        Currently returns all active rules for the role.
        """
        return await self.correction_rule_storage.list_by_role(role_id, active_only=True)

    async def get_rule(self, rule_id: UUID) -> Optional[CorrectionRule]:
        """Get a correction rule by ID"""
        return await self.correction_rule_storage.get_by_id(rule_id)

    async def deactivate_rule(self, rule_id: UUID) -> bool:
        """Deactivate a correction rule"""
        return await self.correction_rule_storage.deactivate(rule_id)
