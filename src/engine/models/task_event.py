"""
TaskEvent Model

Audit trail for task changes (created, took, marked_done, accepted,
rejected, deadline_*, assignee_changed, project_changed, cancelled, overdue).

Rendered as 'History' in the task expand-row UI. NOT a chat message.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class TaskEvent:
    id: UUID = field(default_factory=uuid4)
    task_id: UUID = field(default_factory=uuid4)
    actor_user_id: Optional[UUID] = None
    event_type: str = ""
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "task_id": str(self.task_id),
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }
