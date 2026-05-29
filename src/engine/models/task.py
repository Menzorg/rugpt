"""
Task Model

Employee tasks managed by AI roles.
Created via chat (@@mention) or UI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


VALID_STATUSES = {"created", "in_progress", "awaiting_review", "done", "overdue"}


@dataclass
class Task:
    """
    Employee task.

    Statuses: created, in_progress, awaiting_review, done, overdue.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    title: str = ""
    description: Optional[str] = None
    status: str = "created"
    assignee_user_id: UUID = field(default_factory=uuid4)
    created_by_user_id: Optional[UUID] = None
    deadline: Optional[datetime] = None
    awaiting_review_at: Optional[datetime] = None
    proposed_deadline: Optional[datetime] = None
    proposed_deadline_by: Optional[UUID] = None
    project_id: Optional[UUID] = None
    priority: int = 1
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
            "created_by_user_id": str(self.created_by_user_id) if self.created_by_user_id else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "awaiting_review_at": self.awaiting_review_at.isoformat() if self.awaiting_review_at else None,
            "proposed_deadline": self.proposed_deadline.isoformat() if self.proposed_deadline else None,
            "proposed_deadline_by": str(self.proposed_deadline_by) if self.proposed_deadline_by else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "priority": self.priority,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
