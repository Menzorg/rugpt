"""
Task Poll Model

Daily morning polls for employees to report task status.
"""
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List
from uuid import UUID, uuid4


@dataclass
class TaskPoll:
    """
    Daily morning poll for an employee.

    Statuses: pending, completed, expired.
    Responses: list of {task_id, new_status, comment} per task.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    assignee_user_id: UUID = field(default_factory=uuid4)
    poll_date: date = field(default_factory=date.today)
    status: str = "pending"                         # pending | completed | expired
    responses: list = field(default_factory=list)    # [{task_id, new_status, comment}]
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "assignee_user_id": str(self.assignee_user_id),
            "poll_date": self.poll_date.isoformat(),
            "status": self.status,
            "responses": self.responses,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
