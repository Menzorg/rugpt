"""
Chat Read State Storage

PostgreSQL storage for high-water-mark per (chat, user) used for unread
message counting.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Dict
from uuid import UUID

from .base import BaseStorage

logger = get_logger("storage")

class ChatReadStateStorage(BaseStorage):
    """Storage for chat_read_state high-water-marks."""

    async def upsert(
        self,
        chat_id: UUID,
        user_id: UUID,
        last_read_message_id: UUID,
        last_read_at: datetime,
    ) -> None:
        """UPSERT high-water-mark for (chat_id, user_id).

        Monotonic guard: never moves the high-water-mark backward — the WHERE
        clause on the ON CONFLICT path drops updates whose `last_read_at` is
        older than the currently stored value.
        """
        query = """
            INSERT INTO chat_read_state (chat_id, user_id, last_read_message_id, last_read_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, user_id) DO UPDATE
            SET last_read_message_id = EXCLUDED.last_read_message_id,
                last_read_at = EXCLUDED.last_read_at,
                updated_at = NOW()
            WHERE EXCLUDED.last_read_at >= chat_read_state.last_read_at
        """
        await self.execute(query, chat_id, user_id, last_read_message_id, last_read_at)

    async def get_unread_counts_for_user(
        self, user_id: UUID, org_id: UUID,
    ) -> Dict[UUID, int]:
        """Return {chat_id: unread_count} for all chats of `user_id` in `org_id`.

        - Only chats with count > 0 are included.
        - Counter caps at 100.
        - Excludes the user's own messages and soft-deleted messages.
        """
        query = """
            SELECT c.id AS chat_id,
                   LEAST(COUNT(m.id), 100) AS unread
            FROM chats c
            LEFT JOIN chat_read_state crs ON crs.chat_id = c.id AND crs.user_id = $1
            LEFT JOIN messages m
              ON m.chat_id = c.id
             AND m.sender_id != $1
             AND m.is_deleted = false
             AND m.created_at > COALESCE(crs.last_read_at, '-infinity'::timestamptz)
            WHERE c.org_id = $2
              AND c.is_active = true
              AND $1::text = ANY(c.participants)
            GROUP BY c.id
            HAVING COUNT(m.id) > 0
        """
        rows = await self.fetch(query, user_id, org_id)
        return {row["chat_id"]: int(row["unread"]) for row in rows}
