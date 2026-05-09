"""
Project Storage

PostgreSQL CRUD for projects table.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from .base import BaseStorage
from ..models.project import Project

logger = get_logger("storage")

class ProjectStorage(BaseStorage):

    async def create(self, project: Project) -> Project:
        query = """
            INSERT INTO projects
                (id, org_id, name, description, created_by_user_id,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            project.id, project.org_id, project.name, project.description,
            project.created_by_user_id, project.is_active,
            project.created_at, project.updated_at,
        )
        return self._row_to_project(row)

    async def get_by_id(self, project_id: UUID) -> Optional[Project]:
        row = await self.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
        return self._row_to_project(row) if row else None

    async def list_by_org(
        self, org_id: UUID, include_archived: bool = False,
    ) -> List[Project]:
        if include_archived:
            query = """
                SELECT * FROM projects
                WHERE org_id = $1
                ORDER BY created_at DESC
            """
        else:
            query = """
                SELECT * FROM projects
                WHERE org_id = $1 AND is_active = true
                ORDER BY created_at DESC
            """
        rows = await self.fetch(query, org_id)
        return [self._row_to_project(r) for r in rows]

    async def update(self, project: Project) -> Project:
        project.updated_at = datetime.utcnow()
        query = """
            UPDATE projects
            SET name = $2, description = $3, is_active = $4, updated_at = $5
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            project.id, project.name, project.description,
            project.is_active, project.updated_at,
        )
        return self._row_to_project(row)

    async def deactivate(self, project_id: UUID) -> bool:
        result = await self.execute(
            "UPDATE projects SET is_active = false, updated_at = $2 WHERE id = $1",
            project_id, datetime.utcnow(),
        )
        return "UPDATE 1" in result

    async def get_many_by_ids(self, ids: List[UUID]) -> Dict[UUID, Project]:
        if not ids:
            return {}
        rows = await self.fetch(
            "SELECT * FROM projects WHERE id = ANY($1::uuid[])", list(ids),
        )
        return {row["id"]: self._row_to_project(row) for row in rows}

    def _row_to_project(self, row) -> Project:
        return Project(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            description=row["description"],
            created_by_user_id=row["created_by_user_id"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
