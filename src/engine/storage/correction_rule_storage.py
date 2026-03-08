"""
Correction Rule Storage

PostgreSQL storage for correction rules.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.correction_rule import CorrectionRule

logger = logging.getLogger("rugpt.storage.correction_rule")


class CorrectionRuleStorage(BaseStorage):
    """Storage for CorrectionRule entities"""

    async def create(self, rule: CorrectionRule) -> CorrectionRule:
        """Create a new correction rule"""
        query = """
            INSERT INTO correction_rules (
                id, role_id, org_id, original_message_id, ai_message_id,
                chat_id, user_question, ai_answer, correction_text, rule_text,
                created_by_user_id, is_active, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            rule.id, rule.role_id, rule.org_id,
            rule.original_message_id, rule.ai_message_id,
            rule.chat_id, rule.user_question, rule.ai_answer,
            rule.correction_text, rule.rule_text,
            rule.created_by_user_id, rule.is_active,
            rule.created_at, rule.updated_at,
        )
        return self._row_to_rule(row)

    async def get_by_id(self, rule_id: UUID) -> Optional[CorrectionRule]:
        """Get rule by ID"""
        query = "SELECT * FROM correction_rules WHERE id = $1"
        row = await self.fetchrow(query, rule_id)
        return self._row_to_rule(row) if row else None

    async def list_by_role(
        self, role_id: UUID, active_only: bool = True
    ) -> List[CorrectionRule]:
        """List rules for a role"""
        if active_only:
            query = """
                SELECT * FROM correction_rules
                WHERE role_id = $1 AND is_active = true
                ORDER BY created_at DESC
            """
        else:
            query = """
                SELECT * FROM correction_rules
                WHERE role_id = $1
                ORDER BY created_at DESC
            """
        rows = await self.fetch(query, role_id)
        return [self._row_to_rule(row) for row in rows]

    async def update_rule_text(self, rule_id: UUID, rule_text: str) -> Optional[CorrectionRule]:
        """Update the generated rule_text for a correction rule"""
        query = """
            UPDATE correction_rules
            SET rule_text = $2, updated_at = $3
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(query, rule_id, rule_text, datetime.utcnow())
        return self._row_to_rule(row) if row else None

    async def deactivate(self, rule_id: UUID) -> bool:
        """Deactivate a rule"""
        query = """
            UPDATE correction_rules
            SET is_active = false, updated_at = $2
            WHERE id = $1
        """
        result = await self.execute(query, rule_id, datetime.utcnow())
        return "UPDATE 1" in result

    def _row_to_rule(self, row) -> CorrectionRule:
        """Convert database row to CorrectionRule"""
        return CorrectionRule(
            id=row["id"],
            role_id=row["role_id"],
            org_id=row["org_id"],
            original_message_id=row["original_message_id"],
            ai_message_id=row["ai_message_id"],
            chat_id=row["chat_id"],
            user_question=row["user_question"],
            ai_answer=row["ai_answer"],
            correction_text=row["correction_text"],
            rule_text=row["rule_text"],
            created_by_user_id=row["created_by_user_id"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
