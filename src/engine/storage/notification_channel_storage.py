"""
Notification Channel Storage

PostgreSQL storage for user notification channels.
"""
import json
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.notification import NotificationChannel

logger = logging.getLogger("rugpt.storage.notification_channel")


class NotificationChannelStorage(BaseStorage):
    """Storage for NotificationChannel entities"""

    async def create(self, channel: NotificationChannel) -> NotificationChannel:
        """Create a new notification channel"""
        query = """
            INSERT INTO notification_channels (
                id, user_id, org_id, channel_type, config,
                is_enabled, is_verified, priority,
                created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            channel.id, channel.user_id, channel.org_id,
            channel.channel_type, json.dumps(channel.config),
            channel.is_enabled, channel.is_verified, channel.priority,
            channel.created_at, channel.updated_at
        )
        return self._row_to_channel(row)

    async def get_by_user_and_type(
        self, user_id: UUID, channel_type: str
    ) -> Optional[NotificationChannel]:
        """Get channel by user and type"""
        query = """
            SELECT * FROM notification_channels
            WHERE user_id = $1 AND channel_type = $2
        """
        row = await self.fetchrow(query, user_id, channel_type)
        return self._row_to_channel(row) if row else None

    async def list_by_user(
        self, user_id: UUID, enabled_only: bool = True
    ) -> List[NotificationChannel]:
        """List channels for a user, sorted by priority (highest first)"""
        if enabled_only:
            query = """
                SELECT * FROM notification_channels
                WHERE user_id = $1 AND is_enabled = true
                ORDER BY priority DESC
            """
        else:
            query = """
                SELECT * FROM notification_channels
                WHERE user_id = $1
                ORDER BY priority DESC
            """
        rows = await self.fetch(query, user_id)
        return [self._row_to_channel(row) for row in rows]

    async def update(self, channel: NotificationChannel) -> NotificationChannel:
        """Update notification channel"""
        channel.updated_at = datetime.utcnow()
        query = """
            UPDATE notification_channels
            SET config = $2, is_enabled = $3, is_verified = $4,
                priority = $5, updated_at = $6
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            channel.id, json.dumps(channel.config),
            channel.is_enabled, channel.is_verified,
            channel.priority, channel.updated_at
        )
        return self._row_to_channel(row)

    async def delete_by_user_and_type(self, user_id: UUID, channel_type: str) -> bool:
        """Delete channel by user and type"""
        query = """
            DELETE FROM notification_channels
            WHERE user_id = $1 AND channel_type = $2
        """
        result = await self.execute(query, user_id, channel_type)
        return "DELETE 1" in result

    def _row_to_channel(self, row) -> NotificationChannel:
        """Convert database row to NotificationChannel"""
        config = row["config"] if row["config"] else {}
        if isinstance(config, str):
            config = json.loads(config)

        return NotificationChannel(
            id=row["id"],
            user_id=row["user_id"],
            org_id=row["org_id"],
            channel_type=row["channel_type"],
            config=config,
            is_enabled=row["is_enabled"],
            is_verified=row["is_verified"],
            priority=row["priority"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
