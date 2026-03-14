"""
User File Storage

PostgreSQL CRUD for user_files table (metadata only).
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.user_file import UserFile

logger = logging.getLogger("rugpt.storage.user_file")


class UserFileStorage(BaseStorage):

    async def create(self, file: UserFile) -> UserFile:
        """Create a new file metadata record"""
        query = """
            INSERT INTO user_files
                (id, user_id, org_id, uploaded_by_user_id,
                 storage_key, original_filename, file_type,
                 file_size, content_hash, rag_status,
                 is_active, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            file.id, file.user_id, file.org_id, file.uploaded_by_user_id,
            file.storage_key, file.original_filename, file.file_type,
            file.file_size, file.content_hash, file.rag_status,
            file.is_active, file.created_at, file.updated_at,
        )
        return self._row_to_file(row)

    async def find_duplicate(self, user_id: UUID, content_hash: str) -> Optional[UserFile]:
        """
        Найти активный файл пользователя с совпадающим SHA-256 хешем.

        Используется для детекции дубликатов перед загрузкой:
        если метод вернул запись — файл с идентичным содержимым уже существует.

        Args:
            user_id: владелец файла
            content_hash: SHA-256 hex-дайджест загружаемого файла

        Returns:
            UserFile если дубликат найден, иначе None
        """
        row = await self.fetchrow(
            """
            SELECT * FROM user_files
            WHERE user_id = $1
              AND content_hash = $2
              AND is_active = true
            LIMIT 1
            """,
            user_id,
            content_hash,
        )
        return self._row_to_file(row) if row else None

    async def get_by_id(self, file_id: UUID) -> Optional[UserFile]:
        """Get file by ID"""
        row = await self.fetchrow(
            "SELECT * FROM user_files WHERE id = $1 AND is_active = true",
            file_id,
        )
        return self._row_to_file(row) if row else None

    async def list_by_user(self, user_id: UUID) -> List[UserFile]:
        """List files belonging to an employee"""
        query = """
            SELECT * FROM user_files
            WHERE user_id = $1 AND is_active = true
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, user_id)
        return [self._row_to_file(r) for r in rows]

    async def list_by_org(self, org_id: UUID) -> List[UserFile]:
        """List all files in an organization"""
        query = """
            SELECT * FROM user_files
            WHERE org_id = $1 AND is_active = true
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, org_id)
        return [self._row_to_file(r) for r in rows]

    async def list_pending_indexing(self) -> List[UserFile]:
        """List files pending RAG indexation"""
        query = """
            SELECT * FROM user_files
            WHERE rag_status IN ('pending', 'indexing')
              AND is_active = true
            ORDER BY created_at ASC
        """
        rows = await self.fetch(query)
        return [self._row_to_file(r) for r in rows]

    async def update_rag_status(
        self,
        file_id: UUID,
        rag_status: str,
        rag_error: Optional[str] = None,
        indexed_at: Optional[datetime] = None,
    ) -> Optional[UserFile]:
        """Update RAG indexation status"""
        now = datetime.utcnow()
        query = """
            UPDATE user_files SET
                rag_status = $2,
                rag_error = $3,
                indexed_at = $4,
                updated_at = $5
            WHERE id = $1 AND is_active = true
            RETURNING *
        """
        row = await self.fetchrow(query, file_id, rag_status, rag_error, indexed_at, now)
        return self._row_to_file(row) if row else None

    async def deactivate(self, file_id: UUID) -> bool:
        """Soft-delete a file"""
        result = await self.execute(
            "UPDATE user_files SET is_active = false, updated_at = $2 WHERE id = $1",
            file_id, datetime.utcnow(),
        )
        return "UPDATE 1" in result

    def _row_to_file(self, row) -> UserFile:
        """Map asyncpg Record to UserFile"""
        return UserFile(
            id=row["id"],
            user_id=row["user_id"],
            org_id=row["org_id"],
            uploaded_by_user_id=row["uploaded_by_user_id"],
            storage_key=row["storage_key"],
            original_filename=row["original_filename"],
            file_type=row["file_type"],
            file_size=row["file_size"],
            content_hash=row["content_hash"],
            rag_status=row["rag_status"],
            rag_error=row["rag_error"],
            indexed_at=row["indexed_at"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
