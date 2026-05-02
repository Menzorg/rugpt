"""
MessageAttachmentStorage — junction queries.

Stores message ↔ file links with position. Hydrates UserFile on read so callers
get one object per attachment with everything UI needs.
"""
import logging
from typing import Dict, List, Optional
from uuid import UUID

from .base import BaseStorage
from ..models.message_attachment import MessageAttachment
from ..models.user_file import UserFile

logger = logging.getLogger("rugpt.storage.message_attachment")


class MessageAttachmentStorage(BaseStorage):
    """Storage for message ↔ user_file junction rows."""

    async def attach(self, message_id: UUID, file_ids: List[UUID]) -> None:
        """Bulk insert message-file links with position 0..N-1.

        Order preserved by position. Re-running for same (message_id, file_id) is
        a no-op via ON CONFLICT.
        """
        if not file_ids:
            return
        rows = [(message_id, fid, idx) for idx, fid in enumerate(file_ids)]
        async with self.pg_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO message_attachments (message_id, file_id, position)
                VALUES ($1, $2, $3)
                ON CONFLICT (message_id, file_id) DO NOTHING
                """,
                rows,
            )

    async def get_for_message(self, message_id: UUID) -> List[MessageAttachment]:
        """Get attachments for a single message, ordered by position."""
        rows = await self.fetch(
            """
            SELECT
                ma.message_id, ma.file_id, ma.position,
                f.id AS f_id, f.user_id, f.org_id, f.uploaded_by_user_id,
                f.storage_key, f.original_filename, f.file_type, f.file_size,
                f.content_hash, f.summary, f.is_table,
                f.rag_status, f.rag_error, f.indexed_at, f.is_active, f.is_public,
                f.cloned_from_file_id, f.created_at, f.updated_at
            FROM message_attachments ma
            LEFT JOIN user_files f ON f.id = ma.file_id
            WHERE ma.message_id = $1
            ORDER BY ma.position
            """,
            message_id,
        )
        return [self._row_to_attachment(r) for r in rows]

    async def get_for_messages(
        self, message_ids: List[UUID],
    ) -> Dict[UUID, List[MessageAttachment]]:
        """Bulk-fetch attachments for many messages.

        Returns dict {message_id: [attachments-ordered-by-position]}.
        Messages without attachments are absent from the dict (caller should default to []).
        """
        if not message_ids:
            return {}
        rows = await self.fetch(
            """
            SELECT
                ma.message_id, ma.file_id, ma.position,
                f.id AS f_id, f.user_id, f.org_id, f.uploaded_by_user_id,
                f.storage_key, f.original_filename, f.file_type, f.file_size,
                f.content_hash, f.summary, f.is_table,
                f.rag_status, f.rag_error, f.indexed_at, f.is_active, f.is_public,
                f.cloned_from_file_id, f.created_at, f.updated_at
            FROM message_attachments ma
            LEFT JOIN user_files f ON f.id = ma.file_id
            WHERE ma.message_id = ANY($1)
            ORDER BY ma.message_id, ma.position
            """,
            message_ids,
        )
        out: Dict[UUID, List[MessageAttachment]] = {}
        for r in rows:
            att = self._row_to_attachment(r)
            out.setdefault(att.message_id, []).append(att)
        return out

    async def is_file_visible_to_user(
        self,
        file_id: UUID,
        user_id: UUID,
        org_id: UUID,
    ) -> bool:
        """True if user_id is a participant of any chat where file_id is attached.

        Used by /files/{id}/clone permission check: caller can only clone files
        they've actually seen in a chat they participate in.

        Note: chats.participants is TEXT[] (UUIDs as strings), so user_id is
        cast to str for the array membership check — matches the pattern in
        chat_storage.
        """
        async with self.pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM message_attachments ma
                JOIN messages m ON m.id = ma.message_id
                JOIN chats c ON c.id = m.chat_id
                WHERE ma.file_id = $1
                  AND c.org_id = $2
                  AND $3 = ANY(c.participants)
                LIMIT 1
                """,
                file_id, org_id, str(user_id),
            )
        return row is not None

    def _row_to_attachment(self, row) -> MessageAttachment:
        file: Optional[UserFile] = None
        if row["f_id"] is not None:
            file = UserFile(
                id=row["f_id"],
                user_id=row["user_id"],
                org_id=row["org_id"],
                uploaded_by_user_id=row["uploaded_by_user_id"],
                storage_key=row["storage_key"],
                original_filename=row["original_filename"],
                file_type=row["file_type"],
                file_size=row["file_size"],
                content_hash=row["content_hash"],
                summary=row["summary"],
                is_table=row["is_table"],
                is_public=row["is_public"],
                rag_status=row["rag_status"],
                rag_error=row["rag_error"],
                indexed_at=row["indexed_at"],
                is_active=row["is_active"],
                cloned_from_file_id=row["cloned_from_file_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        return MessageAttachment(
            message_id=row["message_id"],
            file_id=row["file_id"],
            position=row["position"],
            file=file,
        )
