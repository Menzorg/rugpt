"""
Calendar Event Model

Represents a scheduled event tied to a role.
Supports one-time and recurring (cron) events.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class CalendarEvent:
    """
    Calendar event entity.

    Events can be:
    - one_time: fires once at scheduled_at, then deactivated
    - recurring: fires on cron_expression schedule, next_trigger_at is precomputed
    """
    id: UUID = field(default_factory=uuid4)
    role_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    title: str = ""
    description: Optional[str] = None
    event_type: str = "one_time"                       # 'one_time' or 'recurring'

    # Scheduling
    scheduled_at: Optional[datetime] = None             # for one_time events
    cron_expression: Optional[str] = None               # for recurring, e.g. "0 10 * * 4"
    next_trigger_at: Optional[datetime] = None          # precomputed next trigger
    last_triggered_at: Optional[datetime] = None
    trigger_count: int = 0

    # Source context
    source_chat_id: Optional[UUID] = None
    source_message_id: Optional[UUID] = None

    # Metadata
    metadata: dict = field(default_factory=dict)
    created_by_user_id: Optional[UUID] = None
    is_active: bool = True

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "role_id": str(self.role_id),
            "org_id": str(self.org_id),
            "title": self.title,
            "description": self.description,
            "event_type": self.event_type,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "cron_expression": self.cron_expression,
            "next_trigger_at": self.next_trigger_at.isoformat() if self.next_trigger_at else None,
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "trigger_count": self.trigger_count,
            "source_chat_id": str(self.source_chat_id) if self.source_chat_id else None,
            "source_message_id": str(self.source_message_id) if self.source_message_id else None,
            "metadata": self.metadata,
            "created_by_user_id": str(self.created_by_user_id) if self.created_by_user_id else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
