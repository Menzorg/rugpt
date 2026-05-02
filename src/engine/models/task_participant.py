"""
TaskParticipant — additional collaborator on a task chat.

Augments (does not replace) tasks.assignee_user_id and created_by_user_id —
those are the canonical owner/executor; participants are extra users granted
visibility into the task chat. Added by head/admin via API.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class TaskParticipant:
    task_id: UUID
    user_id: UUID
    added_at: datetime
    added_by_user_id: Optional[UUID] = None

    def to_dict(self) -> dict:
        return {
            "task_id": str(self.task_id),
            "user_id": str(self.user_id),
            "added_at": self.added_at.isoformat(),
            "added_by_user_id": str(self.added_by_user_id) if self.added_by_user_id is not None else None,
        }
