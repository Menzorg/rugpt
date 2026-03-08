"""
In-App Notification Storage

PostgreSQL CRUD for in_app_notifications table.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.in_app_notification import InAppNotification

logger = logging.getLogger("rugpt.storage.in_app_notification")


class InAppNotificationStorage(BaseStorage):

    async def create(self, notification: InAppNotification) -> InAppNotification:
        """Create a new in-app notification"""
        query = """
            INSERT INTO in_app_notifications
                (id, user_id, org_id, type, title, content,
                 reference_type, reference_id, is_read, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            notification.id,
            notification.user_id,
            notification.org_id,
            notification.type,
            notification.title,
            notification.content,
            notification.reference_type,
            notification.reference_id,
            notification.is_read,
            notification.created_at,
        )
        return self._row_to_notification(row)

    async def get_by_id(self, notification_id: UUID) -> Optional[InAppNotification]:
        """Get notification by ID"""
        row = await self.fetchrow(
            "SELECT * FROM in_app_notifications WHERE id = $1",
            notification_id,
        )
        return self._row_to_notification(row) if row else None

    async def list_by_user(
        self,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[InAppNotification]:
        """List notifications for a user, newest first"""
        if unread_only:
            query = """
                SELECT * FROM in_app_notifications
                WHERE user_id = $1 AND is_read = false
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
            """
        else:
            query = """
                SELECT * FROM in_app_notifications
                WHERE user_id = $1
                ORDER BY is_read ASC, created_at DESC
                LIMIT $2 OFFSET $3
            """
        rows = await self.fetch(query, user_id, limit, offset)
        return [self._row_to_notification(r) for r in rows]

    async def count_unread(self, user_id: UUID) -> int:
        """Count unread notifications for a user"""
        count = await self.fetchval(
            "SELECT COUNT(*) FROM in_app_notifications WHERE user_id = $1 AND is_read = false",
            user_id,
        )
        return count or 0

    async def mark_read(self, notification_id: UUID) -> bool:
        """Mark a single notification as read"""
        result = await self.execute(
            "UPDATE in_app_notifications SET is_read = true WHERE id = $1 AND is_read = false",
            notification_id,
        )
        return "UPDATE 1" in result

    async def mark_all_read(self, user_id: UUID) -> int:
        """Mark all notifications as read for a user. Returns count of updated."""
        result = await self.execute(
            "UPDATE in_app_notifications SET is_read = true WHERE user_id = $1 AND is_read = false",
            user_id,
        )
        # result is like "UPDATE 5"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    def _row_to_notification(self, row) -> InAppNotification:
        """Map asyncpg Record to InAppNotification"""
        return InAppNotification(
            id=row["id"],
            user_id=row["user_id"],
            org_id=row["org_id"],
            type=row["type"],
            title=row["title"],
            content=row["content"],
            reference_type=row["reference_type"],
            reference_id=row["reference_id"],
            is_read=row["is_read"],
            created_at=row["created_at"],
        )
