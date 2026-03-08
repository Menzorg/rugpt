"""
Task Model

Employee tasks managed by AI roles.
Created via chat (@@mention) or UI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Task:
    """
    Employee task.

    Statuses: created, in_progress, done, overdue.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    title: str = ""
    description: Optional[str] = None
    status: str = "created"                         # created | in_progress | done | overdue
    assignee_user_id: UUID = field(default_factory=uuid4)
    deadline: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "assignee_user_id": str(self.assignee_user_id),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
