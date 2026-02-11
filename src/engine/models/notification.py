"""
Notification Models

NotificationChannel: user's delivery channel config (telegram, email, chat).
NotificationLog: delivery attempt log.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class NotificationChannel:
    """
    A user's notification delivery channel.

    Each user can have one channel per type (UNIQUE user_id + channel_type).
    Channels are tried in priority order (higher = first).
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    channel_type: str = ""                              # 'telegram', 'email', 'chat'
    config: dict = field(default_factory=dict)           # {chat_id: "..."} or {email: "..."}
    is_enabled: bool = True
    is_verified: bool = False
    priority: int = 0                                    # higher = tried first

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "channel_type": self.channel_type,
            "config": self.config,
            "is_enabled": self.is_enabled,
            "is_verified": self.is_verified,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class NotificationLog:
    """
    Log entry for a notification delivery attempt.
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    channel_type: str = ""
    event_id: Optional[UUID] = None
    role_id: Optional[UUID] = None
    content: str = ""
    status: str = "pending"                              # 'pending', 'sent', 'failed'
    attempts: int = 0
    error_message: Optional[str] = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "channel_type": self.channel_type,
            "event_id": str(self.event_id) if self.event_id else None,
            "role_id": str(self.role_id) if self.role_id else None,
            "content": self.content,
            "status": self.status,
            "attempts": self.attempts,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
