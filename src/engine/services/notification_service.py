"""
Notification Service

Orchestrates notification delivery across channels.
Tries channels by priority (highest first), logs all attempts.
"""
import logging
from typing import Optional, List, Dict
from uuid import UUID

from ..models.notification import NotificationChannel, NotificationLog
from ..storage.notification_channel_storage import NotificationChannelStorage
from ..storage.notification_log_storage import NotificationLogStorage
from ..notifications.base_sender import BaseSender, SendResult

logger = logging.getLogger("rugpt.services.notification")


class NotificationService:
    """
    Notification delivery orchestrator.

    For each user notification:
    1. Loads user's enabled channels, sorted by priority (desc)
    2. Tries each channel's sender
    3. Logs attempt in notification_log
    4. Stops on first successful delivery
    """

    def __init__(
        self,
        channel_storage: NotificationChannelStorage,
        log_storage: NotificationLogStorage,
        senders: Optional[Dict[str, BaseSender]] = None,
    ):
        self.channel_storage = channel_storage
        self.log_storage = log_storage
        # channel_type -> sender instance
        self._senders: Dict[str, BaseSender] = senders or {}

    def register_sender(self, channel_type: str, sender: BaseSender):
        """Register a sender for a channel type"""
        self._senders[channel_type] = sender
        logger.info(f"Registered notification sender: {channel_type}")

    async def send_notification(
        self,
        user_id: UUID,
        content: str,
        event_id: Optional[UUID] = None,
        role_id: Optional[UUID] = None,
    ) -> bool:
        """
        Send notification to user via their configured channels.

        Tries channels by priority (highest first).
        Returns True if at least one channel succeeded.
        """
        channels = await self.channel_storage.list_by_user(user_id, enabled_only=True)

        if not channels:
            logger.warning(f"No notification channels for user {user_id}")
            return False

        for channel in channels:
            sender = self._senders.get(channel.channel_type)
            if not sender:
                logger.warning(
                    f"No sender registered for channel type '{channel.channel_type}'"
                )
                continue

            if not channel.is_verified:
                logger.debug(
                    f"Skipping unverified channel {channel.channel_type} for user {user_id}"
                )
                continue

            # Create log entry
            log_entry = NotificationLog(
                user_id=user_id,
                channel_type=channel.channel_type,
                event_id=event_id,
                role_id=role_id,
                content=content,
                status="pending",
            )
            log_entry = await self.log_storage.create(log_entry)

            # Attempt to send
            result = await sender.send(channel.config, content)

            if result.success:
                await self.log_storage.update_status(
                    log_entry.id, status="sent", attempts=1
                )
                logger.info(
                    f"Notification sent to user {user_id} via {channel.channel_type}"
                )
                return True
            else:
                await self.log_storage.update_status(
                    log_entry.id,
                    status="failed",
                    attempts=1,
                    error_message=result.error,
                )
                logger.warning(
                    f"Failed to send via {channel.channel_type} for user {user_id}: "
                    f"{result.error}"
                )

        logger.error(f"All notification channels failed for user {user_id}")
        return False

    async def send_to_multiple_users(
        self,
        user_ids: List[UUID],
        content: str,
        event_id: Optional[UUID] = None,
        role_id: Optional[UUID] = None,
    ) -> Dict[str, bool]:
        """
        Send notification to multiple users.

        Returns dict of {user_id_str: success_bool}.
        """
        results = {}
        for uid in user_ids:
            results[str(uid)] = await self.send_notification(
                uid, content, event_id, role_id
            )
        return results

    # ============================================
    # Channel management helpers
    # ============================================

    async def register_channel(
        self,
        user_id: UUID,
        org_id: UUID,
        channel_type: str,
        config: dict,
        priority: int = 0,
    ) -> NotificationChannel:
        """Register or update a notification channel for a user"""
        existing = await self.channel_storage.get_by_user_and_type(
            user_id, channel_type
        )

        if existing:
            existing.config = config
            existing.priority = priority
            existing.is_enabled = True
            return await self.channel_storage.update(existing)

        channel = NotificationChannel(
            user_id=user_id,
            org_id=org_id,
            channel_type=channel_type,
            config=config,
            priority=priority,
        )
        return await self.channel_storage.create(channel)

    async def verify_channel(
        self, user_id: UUID, channel_type: str
    ) -> Optional[NotificationChannel]:
        """Mark a channel as verified"""
        channel = await self.channel_storage.get_by_user_and_type(
            user_id, channel_type
        )
        if not channel:
            return None
        channel.is_verified = True
        return await self.channel_storage.update(channel)

    async def get_user_channels(
        self, user_id: UUID, enabled_only: bool = False
    ) -> List[NotificationChannel]:
        """Get all channels for a user"""
        return await self.channel_storage.list_by_user(user_id, enabled_only)

    async def remove_channel(self, user_id: UUID, channel_type: str) -> bool:
        """Remove a notification channel"""
        return await self.channel_storage.delete_by_user_and_type(
            user_id, channel_type
        )

    async def get_notification_log(
        self, user_id: UUID, limit: int = 50
    ) -> List[NotificationLog]:
        """Get notification log for a user"""
        return await self.log_storage.list_by_user(user_id, limit)

    async def close(self):
        """Cleanup sender resources"""
        for sender in self._senders.values():
            await sender.close()
