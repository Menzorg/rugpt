"""
Device Storage

CRUD operations for user_devices table (Zero Trust).
"""
import logging
from typing import Optional, List, Dict, Any
from uuid import UUID

from .base import BaseStorage

logger = logging.getLogger("rugpt.storage.device")


class DeviceStorage(BaseStorage):
    """Storage for user device public keys"""

    async def register_device(
        self,
        user_id: UUID,
        device_public_key: str,
        device_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        Register device public key. Idempotent via ON CONFLICT.
        Returns device_id.
        """
        row = await self.fetchrow(
            """
            INSERT INTO user_devices (user_id, device_public_key, device_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, device_public_key)
            DO UPDATE SET last_used_at = NOW(), device_name = COALESCE($3, user_devices.device_name)
            RETURNING id
            """,
            user_id, device_public_key, device_name,
        )
        if row:
            logger.info(f"Device registered for user {user_id}: {row['id']}")
            return str(row["id"])
        return None

    async def get_device_public_key(
        self,
        user_id: UUID,
        device_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get device public key for signature verification.
        If device_id not specified, returns most recently used active device.
        """
        if device_id:
            row = await self.fetchrow(
                """
                SELECT device_public_key FROM user_devices
                WHERE id = $1 AND user_id = $2 AND is_active = true
                """,
                UUID(device_id), user_id,
            )
        else:
            row = await self.fetchrow(
                """
                SELECT device_public_key FROM user_devices
                WHERE user_id = $1 AND is_active = true
                ORDER BY last_used_at DESC
                LIMIT 1
                """,
                user_id,
            )
        return row["device_public_key"] if row else None

    async def get_all_public_keys(self, user_id: UUID) -> List[str]:
        """Get all active device public keys for a user."""
        rows = await self.fetch(
            """
            SELECT device_public_key FROM user_devices
            WHERE user_id = $1 AND is_active = true
            ORDER BY last_used_at DESC
            """,
            user_id,
        )
        return [row["device_public_key"] for row in rows]

    async def update_last_used(self, user_id: UUID, device_public_key: str) -> bool:
        """Update last_used_at timestamp."""
        result = await self.execute(
            """
            UPDATE user_devices SET last_used_at = NOW()
            WHERE user_id = $1 AND device_public_key = $2 AND is_active = true
            """,
            user_id, device_public_key,
        )
        return "UPDATE 1" in result

    async def get_user_devices(self, user_id: UUID) -> List[Dict[str, Any]]:
        """List all devices for a user."""
        rows = await self.fetch(
            """
            SELECT id, device_name, is_active, created_at, last_used_at
            FROM user_devices WHERE user_id = $1
            ORDER BY last_used_at DESC
            """,
            user_id,
        )
        return [dict(row) for row in rows]

    async def deactivate_device(self, user_id: UUID, device_id: UUID) -> bool:
        """Deactivate a device."""
        result = await self.execute(
            "UPDATE user_devices SET is_active = false WHERE id = $1 AND user_id = $2",
            device_id, user_id,
        )
        return "UPDATE 1" in result
