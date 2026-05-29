"""
User File Folder Storage

PostgreSQL CRUD for user_file_folders.
Recursive CTE for subtree reads (capped at depth 20).
"""
import logging
from typing import List, Optional, Set
from uuid import UUID

from .base import BaseStorage
from ..models.user_file_folder import UserFileFolder

logger = logging.getLogger("rugpt.storage.user_file_folder")


class UserFileFolderStorage(BaseStorage):

    async def create(self, folder: UserFileFolder) -> UserFileFolder:
        """Insert a new folder. Raises asyncpg.UniqueViolationError on name conflict."""
        row = await self.fetchrow(
            """
            INSERT INTO user_file_folders
                (id, user_id, org_id, parent_folder_id, name,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            folder.id, folder.user_id, folder.org_id, folder.parent_folder_id,
            folder.name, folder.is_active, folder.created_at, folder.updated_at,
        )
        return self._row_to_folder(row)

    async def get_by_id(self, folder_id: UUID) -> Optional[UserFileFolder]:
        row = await self.fetchrow(
            "SELECT * FROM user_file_folders WHERE id = $1 AND is_active = true",
            folder_id,
        )
        return self._row_to_folder(row) if row else None

    async def list_by_user(self, user_id: UUID) -> List[UserFileFolder]:
        rows = await self.fetch(
            """
            SELECT * FROM user_file_folders
            WHERE user_id = $1 AND is_active = true
            ORDER BY parent_folder_id NULLS FIRST, lower(name)
            """,
            user_id,
        )
        return [self._row_to_folder(r) for r in rows]

    async def list_children(
        self, user_id: UUID, parent_folder_id: Optional[UUID],
    ) -> List[UserFileFolder]:
        rows = await self.fetch(
            """
            SELECT * FROM user_file_folders
            WHERE user_id = $1
              AND parent_folder_id IS NOT DISTINCT FROM $2
              AND is_active = true
            ORDER BY lower(name)
            """,
            user_id, parent_folder_id,
        )
        return [self._row_to_folder(r) for r in rows]

    async def list_subtree_ids(self, folder_id: UUID) -> Set[UUID]:
        """Self + all active descendants. Hard-cap depth < 20."""
        rows = await self.fetch(
            """
            WITH RECURSIVE subtree AS (
                SELECT id, 0 AS depth FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, s.depth + 1
                FROM user_file_folders f
                JOIN subtree s ON f.parent_folder_id = s.id
                WHERE f.is_active = true AND s.depth < 20
            )
            SELECT id FROM subtree
            """,
            folder_id,
        )
        return {r["id"] for r in rows}

    async def deactivate_subtree(self, folder_id: UUID) -> List[UUID]:
        """Soft-delete self + all active descendants. Returns deactivated ids."""
        rows = await self.fetch(
            """
            WITH RECURSIVE subtree AS (
                SELECT id, 0 AS depth FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, s.depth + 1
                FROM user_file_folders f
                JOIN subtree s ON f.parent_folder_id = s.id
                WHERE f.is_active = true AND s.depth < 20
            )
            UPDATE user_file_folders f
            SET is_active = false, updated_at = NOW()
            FROM subtree s
            WHERE f.id = s.id
            RETURNING f.id
            """,
            folder_id,
        )
        return [r["id"] for r in rows]

    async def update(self, folder: UserFileFolder) -> Optional[UserFileFolder]:
        """Update name and/or parent_folder_id. Raises UniqueViolationError on conflict."""
        row = await self.fetchrow(
            """
            UPDATE user_file_folders
            SET name = $2,
                parent_folder_id = $3,
                updated_at = NOW()
            WHERE id = $1 AND is_active = true
            RETURNING *
            """,
            folder.id, folder.name, folder.parent_folder_id,
        )
        return self._row_to_folder(row) if row else None

    async def get_depth(self, folder_id: UUID) -> Optional[int]:
        """Compute depth of a folder by walking parent_folder_id chain up to root. None if not active."""
        return await self.fetchval(
            """
            WITH RECURSIVE up AS (
                SELECT id, parent_folder_id, 0 AS depth
                FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, f.parent_folder_id, up.depth + 1
                FROM user_file_folders f
                JOIN up ON up.parent_folder_id = f.id
                WHERE f.is_active = true AND up.depth < 20
            )
            SELECT MAX(depth) FROM up
            """,
            folder_id,
        )

    async def get_subtree_max_depth(self, folder_id: UUID) -> int:
        """Max depth from folder down to its deepest active descendant (0 if no children)."""
        val = await self.fetchval(
            """
            WITH RECURSIVE down AS (
                SELECT id, 0 AS d FROM user_file_folders
                WHERE id = $1 AND is_active = true
                UNION ALL
                SELECT f.id, d.d + 1 FROM user_file_folders f
                JOIN down d ON f.parent_folder_id = d.id
                WHERE f.is_active = true AND d.d < 20
            )
            SELECT MAX(d) FROM down
            """,
            folder_id,
        )
        return int(val) if val is not None else 0

    def _row_to_folder(self, row) -> UserFileFolder:
        return UserFileFolder(
            id=row["id"],
            user_id=row["user_id"],
            org_id=row["org_id"],
            parent_folder_id=row["parent_folder_id"],
            name=row["name"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
