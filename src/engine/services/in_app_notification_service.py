"""
In-App Notification Service

Business logic for bell icon notifications.
Used by task_service, task_poll_service, task_report_service, mention_service.
"""
import logging
from typing import Optional, List
from uuid import UUID

from ..models.in_app_notification import InAppNotification
from ..storage.in_app_notification_storage import InAppNotificationStorage

logger = logging.getLogger("rugpt.services.in_app_notification")


class InAppNotificationService:

    def __init__(self, storage: InAppNotificationStorage):
        self.storage = storage

    async def create(
        self,
        user_id: UUID,
        org_id: UUID,
        type: str,
        title: str,
        content: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[UUID] = None,
    ) -> InAppNotification:
        """Create a new in-app notification"""
        valid_types = {"new_task", "poll", "report", "mention", "task_status_change", "system"}
        if type not in valid_types:
            raise ValueError(f"Invalid notification type: {type}. Must be one of {valid_types}")

        if not title:
            raise ValueError("Notification title is required")

        notification = InAppNotification(
            user_id=user_id,
            org_id=org_id,
            type=type,
            title=title,
            content=content,
            reference_type=reference_type,
            reference_id=reference_id,
        )
        created = await self.storage.create(notification)
        logger.info(f"Created notification [{type}] for user {user_id}: {title}")
        return created

    async def get(self, notification_id: UUID) -> Optional[InAppNotification]:
        """Get notification by ID"""
        return await self.storage.get_by_id(notification_id)

    async def list_for_user(
        self,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
    ) -> List[InAppNotification]:
        """List notifications for a user"""
        return await self.storage.list_by_user(user_id, limit, offset, unread_only)

    async def count_unread(self, user_id: UUID) -> int:
        """Get unread notification count for bell badge"""
        return await self.storage.count_unread(user_id)

    async def mark_read(self, notification_id: UUID) -> bool:
        """Mark a single notification as read"""
        success = await self.storage.mark_read(notification_id)
        if success:
            logger.debug(f"Marked notification {notification_id} as read")
        return success

    async def mark_all_read(self, user_id: UUID) -> int:
        """Mark all notifications as read for a user"""
        count = await self.storage.mark_all_read(user_id)
        if count > 0:
            logger.info(f"Marked {count} notifications as read for user {user_id}")
        return count
