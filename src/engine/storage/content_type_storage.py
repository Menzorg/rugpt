"""
Content Type Storage

PostgreSQL CRUD for content_types (admin-managed per-org file categories).
Soft-delete via is_active. See migration 051.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.content_type import ContentType

logger = get_logger("storage")


class ContentTypeStorage(BaseStorage):

    async def create(self, ct: ContentType) -> ContentType:
        row = await self.fetchrow(
            """
            INSERT INTO content_types (id, org_id, name, description, is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            ct.id, ct.org_id, ct.name, ct.description, ct.is_active, ct.created_at, ct.updated_at,
        )
        return self._row_to_content_type(row)

    async def get_by_id(self, content_type_id: UUID) -> Optional[ContentType]:
        row = await self.fetchrow("SELECT * FROM content_types WHERE id = $1", content_type_id)
        return self._row_to_content_type(row) if row else None

    async def list_by_org(self, org_id: UUID, include_inactive: bool = False) -> List[ContentType]:
        """List content types for an org. Picker uses active-only; admin settings
        passes include_inactive=True to also show deactivated entries."""
        if include_inactive:
            rows = await self.fetch(
                "SELECT * FROM content_types WHERE org_id = $1 ORDER BY is_active DESC, name",
                org_id,
            )
        else:
            rows = await self.fetch(
                "SELECT * FROM content_types WHERE org_id = $1 AND is_active = true ORDER BY name",
                org_id,
            )
        return [self._row_to_content_type(r) for r in rows]

    async def update(
        self,
        content_type_id: UUID,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[ContentType]:
        """Partial update — only non-None fields change (COALESCE)."""
        row = await self.fetchrow(
            """
            UPDATE content_types
            SET name = COALESCE($2, name),
                description = COALESCE($3, description),
                is_active = COALESCE($4, is_active),
                updated_at = $5
            WHERE id = $1
            RETURNING *
            """,
            content_type_id, name, description, is_active, datetime.utcnow(),
        )
        return self._row_to_content_type(row) if row else None

    async def soft_delete(self, content_type_id: UUID) -> bool:
        result = await self.execute(
            "UPDATE content_types SET is_active = false, updated_at = $2 WHERE id = $1 AND is_active = true",
            content_type_id, datetime.utcnow(),
        )
        return "UPDATE 1" in result

    async def search_by_name(
        self,
        org_id: UUID,
        query: str,
        threshold: float = 0.1,
    ) -> List[ContentType]:
        """Fuzzy trigram search over active content type names for an org.
        Uses search_content_types() SQL function from migration 053."""
        rows = await self.fetch(
            "SELECT * FROM search_content_types($1, $2, $3::real)",
            org_id, query, threshold,
        )
        return [self._row_to_content_type(r) for r in rows]

    def _row_to_content_type(self, row) -> ContentType:
        return ContentType(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            description=row["description"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
