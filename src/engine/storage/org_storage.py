"""
Organization Storage

PostgreSQL storage for organizations.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.organization import Organization

logger = logging.getLogger("rugpt.storage.org")


class OrgStorage(BaseStorage):
    """Storage for Organization entities"""

    async def create(self, org: Organization) -> Organization:
        """Create a new organization"""
        query = """
            INSERT INTO organizations (id, name, slug, description, is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, name, slug, description, is_active, created_at, updated_at
        """
        row = await self.fetchrow(
            query,
            org.id,
            org.name,
            org.slug,
            org.description,
            org.is_active,
            org.created_at,
            org.updated_at
        )
        return self._row_to_org(row)

    async def get_by_id(self, org_id: UUID) -> Optional[Organization]:
        """Get organization by ID"""
        query = """
            SELECT id, name, slug, description, is_active, created_at, updated_at
            FROM organizations
            WHERE id = $1
        """
        row = await self.fetchrow(query, org_id)
        return self._row_to_org(row) if row else None

    async def get_by_slug(self, slug: str) -> Optional[Organization]:
        """Get organization by slug"""
        query = """
            SELECT id, name, slug, description, is_active, created_at, updated_at
            FROM organizations
            WHERE slug = $1
        """
        row = await self.fetchrow(query, slug)
        return self._row_to_org(row) if row else None

    async def list_all(self, active_only: bool = True) -> List[Organization]:
        """List all organizations"""
        if active_only:
            query = """
                SELECT id, name, slug, description, is_active, created_at, updated_at
                FROM organizations
                WHERE is_active = true
                ORDER BY name
            """
        else:
            query = """
                SELECT id, name, slug, description, is_active, created_at, updated_at
                FROM organizations
                ORDER BY name
            """
        rows = await self.fetch(query)
        return [self._row_to_org(row) for row in rows]

    async def update(self, org: Organization) -> Organization:
        """Update organization"""
        org.updated_at = datetime.utcnow()
        query = """
            UPDATE organizations
            SET name = $2, slug = $3, description = $4, is_active = $5, updated_at = $6
            WHERE id = $1
            RETURNING id, name, slug, description, is_active, created_at, updated_at
        """
        row = await self.fetchrow(
            query,
            org.id,
            org.name,
            org.slug,
            org.description,
            org.is_active,
            org.updated_at
        )
        return self._row_to_org(row)

    async def delete(self, org_id: UUID) -> bool:
        """Soft delete organization (set is_active = false)"""
        query = """
            UPDATE organizations
            SET is_active = false, updated_at = $2
            WHERE id = $1
        """
        result = await self.execute(query, org_id, datetime.utcnow())
        return "UPDATE 1" in result

    async def exists_by_slug(self, slug: str, exclude_id: Optional[UUID] = None) -> bool:
        """Check if organization with slug exists"""
        if exclude_id:
            query = "SELECT 1 FROM organizations WHERE slug = $1 AND id != $2"
            result = await self.fetchval(query, slug, exclude_id)
        else:
            query = "SELECT 1 FROM organizations WHERE slug = $1"
            result = await self.fetchval(query, slug)
        return result is not None

    def _row_to_org(self, row) -> Organization:
        """Convert database row to Organization"""
        return Organization(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            description=row["description"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )
