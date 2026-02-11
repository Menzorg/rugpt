"""
Notification Log Storage

PostgreSQL storage for notification delivery log.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.notification import NotificationLog

logger = logging.getLogger("rugpt.storage.notification_log")


class NotificationLogStorage(BaseStorage):
    """Storage for NotificationLog entities"""

    async def create(self, log_entry: NotificationLog) -> NotificationLog:
        """Create a new notification log entry"""
        query = """
            INSERT INTO notification_log (
                id, user_id, channel_type, event_id, role_id,
                content, status, attempts, error_message,
                created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            log_entry.id, log_entry.user_id, log_entry.channel_type,
            log_entry.event_id, log_entry.role_id,
            log_entry.content, log_entry.status, log_entry.attempts,
            log_entry.error_message,
            log_entry.created_at, log_entry.updated_at
        )
        return self._row_to_log(row)

    async def update_status(
        self, log_id: UUID, status: str, attempts: int,
        error_message: Optional[str] = None
    ) -> Optional[NotificationLog]:
        """Update log entry status"""
        query = """
            UPDATE notification_log
            SET status = $2, attempts = $3, error_message = $4, updated_at = $5
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query, log_id, status, attempts, error_message, datetime.utcnow()
        )
        return self._row_to_log(row) if row else None

    async def list_by_user(
        self, user_id: UUID, limit: int = 50
    ) -> List[NotificationLog]:
        """List log entries for a user"""
        query = """
            SELECT * FROM notification_log
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """
        rows = await self.fetch(query, user_id, limit)
        return [self._row_to_log(row) for row in rows]

    async def list_by_event(self, event_id: UUID) -> List[NotificationLog]:
        """List log entries for a specific event"""
        query = """
            SELECT * FROM notification_log
            WHERE event_id = $1
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, event_id)
        return [self._row_to_log(row) for row in rows]

    def _row_to_log(self, row) -> NotificationLog:
        """Convert database row to NotificationLog"""
        return NotificationLog(
            id=row["id"],
            user_id=row["user_id"],
            channel_type=row["channel_type"],
            event_id=row["event_id"],
            role_id=row["role_id"],
            content=row["content"],
            status=row["status"],
            attempts=row["attempts"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
