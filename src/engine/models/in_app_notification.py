"""
In-App Notification Model

Bell icon notifications for tasks, polls, reports, mentions.
Separate from external notification channels (telegram/email).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class InAppNotification:
    """
    In-app bell notification.

    Types: new_task, poll, report, mention, task_status_change, system.
    Reference links to the related entity (task, poll, report, message).
    """
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    type: str = ""                                  # new_task | poll | report | mention | task_status_change | system
    title: str = ""
    content: Optional[str] = None
    reference_type: Optional[str] = None            # task | task_poll | task_report | message
    reference_id: Optional[UUID] = None
    is_read: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "reference_type": self.reference_type,
            "reference_id": str(self.reference_id) if self.reference_id else None,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat(),
        }
