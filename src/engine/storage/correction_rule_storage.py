"""
Correction Rule Storage

PostgreSQL storage for correction rules.
"""
import logging
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
                id, role_id, mem_id, mem_embedding,
                src_user_message_id, user_message_embedding,
                src_ai_response_id, user_correction_text, extracted_lesson
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            rule.id, rule.role_id, rule.mem_id,
            rule.mem_embedding,
            rule.src_user_message_id, rule.user_message_embedding,
            rule.src_ai_response_id, rule.user_correction_text, rule.extracted_lesson,
        )
        return self._row_to_rule(row)

    async def get_by_id(self, rule_id: UUID) -> Optional[CorrectionRule]:
        """Get rule by ID"""
        row = await self.fetchrow("SELECT * FROM correction_rules WHERE id = $1", rule_id)
        return self._row_to_rule(row) if row else None

    async def list_by_role(self, role_id: UUID) -> List[CorrectionRule]:
        """List all correction rules for a role"""
        rows = await self.fetch(
            "SELECT * FROM correction_rules WHERE role_id = $1", role_id
        )
        return [self._row_to_rule(row) for row in rows]

    async def list_by_mem(self, mem_id: UUID) -> List[CorrectionRule]:
        """List all correction rules associated with a memory snapshot"""
        rows = await self.fetch(
            "SELECT * FROM correction_rules WHERE mem_id = $1", mem_id
        )
        return [self._row_to_rule(row) for row in rows]

    async def update_extracted_lesson(self, rule_id: UUID, extracted_lesson: str) -> Optional[CorrectionRule]:
        """Set the extracted lesson on a rule"""
        row = await self.fetchrow(
            """
            UPDATE correction_rules
            SET extracted_lesson = $2
            WHERE id = $1
            RETURNING *
            """,
            rule_id, extracted_lesson,
        )
        return self._row_to_rule(row) if row else None

    async def delete(self, rule_id: UUID) -> bool:
        """Hard delete a correction rule"""
        result = await self.execute(
            "DELETE FROM correction_rules WHERE id = $1", rule_id
        )
        return "DELETE 1" in result

    def _row_to_rule(self, row) -> CorrectionRule:
        """Convert database row to CorrectionRule"""
        return CorrectionRule(
            id=row["id"],
            role_id=row["role_id"],
            mem_id=row["mem_id"],
            mem_embedding=list(row["mem_embedding"]) if row["mem_embedding"] is not None else None,
            src_user_message_id=row["src_user_message_id"],
            user_message_embedding=list(row["user_message_embedding"]) if row["user_message_embedding"] is not None else None,
            src_ai_response_id=row["src_ai_response_id"],
            user_correction_text=row["user_correction_text"],
            extracted_lesson=row["extracted_lesson"],
        )
