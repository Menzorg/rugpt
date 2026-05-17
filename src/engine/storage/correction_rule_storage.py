"""
Correction Rule Storage

PostgreSQL storage for correction rules.
"""

from src.engine.unified_logger import get_logger
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.correction_rule import CorrectionRule

logger = get_logger("storage")

class CorrectionRuleStorage(BaseStorage):
    """Storage for CorrectionRule entities"""

    async def create(self, rule: CorrectionRule) -> CorrectionRule:
        """Create a new correction rule"""
        query = """
            INSERT INTO correction_rules (
                id, role_id, mem_id, mem_embedding,
                src_user_message_id, user_message_embedding,
                src_ai_response_id, user_correction_text, extracted_lesson,
                is_active
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            rule.id, rule.role_id, rule.mem_id,
            rule.mem_embedding,
            rule.src_user_message_id, rule.user_message_embedding,
            rule.src_ai_response_id, rule.user_correction_text, rule.extracted_lesson,
            rule.is_active,
        )
        return self._row_to_rule(row)

    async def get_by_id(self, rule_id: UUID) -> Optional[CorrectionRule]:
        """Get rule by ID"""
        row = await self.fetchrow("SELECT * FROM correction_rules WHERE id = $1", rule_id)
        return self._row_to_rule(row) if row else None

    async def list_active(self) -> List[CorrectionRule]:
        """List all active correction rules."""
        rows = await self.fetch("SELECT * FROM correction_rules WHERE is_active = true")
        return [self._row_to_rule(row) for row in rows]

    async def list_by_role(self, role_id: UUID, active_only: bool = True) -> List[CorrectionRule]:
        """List correction rules for a role"""
        if active_only:
            rows = await self.fetch(
                "SELECT * FROM correction_rules WHERE role_id = $1 AND is_active = true", role_id
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM correction_rules WHERE role_id = $1", role_id
            )
        return [self._row_to_rule(row) for row in rows]

    async def list_by_mem(self, mem_id: UUID, active_only: bool = True) -> List[CorrectionRule]:
        """List correction rules associated with a memory snapshot"""
        if active_only:
            rows = await self.fetch(
                "SELECT * FROM correction_rules WHERE mem_id = $1 AND is_active = true", mem_id
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM correction_rules WHERE mem_id = $1", mem_id
            )
        return [self._row_to_rule(row) for row in rows]

    async def update(self, rule: CorrectionRule) -> Optional[CorrectionRule]:
        """Update all mutable fields of a correction rule."""
        row = await self.fetchrow(
            """
            UPDATE correction_rules
            SET
                role_id              = $2,
                mem_id               = $3,
                mem_embedding        = $4,
                src_user_message_id  = $5,
                user_message_embedding = $6,
                src_ai_response_id   = $7,
                user_correction_text = $8,
                extracted_lesson     = $9,
                is_active            = $10
            WHERE id = $1
            RETURNING *
            """,
            rule.id, rule.role_id, rule.mem_id,
            rule.mem_embedding,
            rule.src_user_message_id, rule.user_message_embedding,
            rule.src_ai_response_id, rule.user_correction_text, rule.extracted_lesson,
            rule.is_active,
        )
        return self._row_to_rule(row) if row else None

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

    async def search_by_embeddings(
        self,
        mem_embedding: List[float],
        user_message_embedding: List[float],
        top_k: int = 5,
        role_id: Optional[UUID] = None,
    ) -> List[CorrectionRule]:
        """Search correction rules by semantic similarity using both memory and user prompt embeddings."""
        mem_literal = "[" + ",".join(str(v) for v in mem_embedding) + "]"
        user_literal = "[" + ",".join(str(v) for v in user_message_embedding) + "]"
        rows = await self.fetch(
            """
            SELECT * FROM search_correction_rules($1::vector, $2::vector, $3, $4::uuid)
            """,
            mem_literal, user_literal, top_k, role_id,
        )
        return [self._row_to_rule(row) for row in rows]

    async def deactivate(self, rule_id: UUID) -> bool:
        """Soft-delete a correction rule by setting is_active = false"""
        result = await self.execute(
            "UPDATE correction_rules SET is_active = false WHERE id = $1", rule_id
        )
        return "UPDATE 1" in result

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
            is_active=row["is_active"],
        )
