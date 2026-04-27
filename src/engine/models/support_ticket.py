"""
SupportTicket Model — tech support request entity.

Lives across orgs: requester is from a customer org, assignee (when assigned)
is from RuGPT Support org. Chat for the ticket has org_id = requester_org_id
and uses ChatType.SUPPORT.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4


class SupportTicketCategory(str, Enum):
    """Support ticket categories — match SQL CHECK in migration 023."""
    HOW_TO = "how_to"
    BUG = "bug"
    OTHER = "other"


class SupportTicketStatus(str, Enum):
    """Lifecycle: open -> in_progress (after take) -> closed."""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class ClosedByRole(str, Enum):
    """Who closed the ticket — requester (self-served) or operator."""
    REQUESTER = "requester"
    OPERATOR = "operator"


@dataclass
class SupportTicket:
    """
    Tech support ticket — created by client, assigned to RuGPT Support operator.

    Lifecycle: open -> in_progress (after take) -> closed (by either party).
    Reopen via message in window is handled by service layer, not this model.
    """
    id: UUID = field(default_factory=uuid4)
    requester_user_id: UUID = field(default_factory=uuid4)
    requester_org_id: UUID = field(default_factory=uuid4)
    category: SupportTicketCategory = SupportTicketCategory.HOW_TO
    status: SupportTicketStatus = SupportTicketStatus.OPEN
    assignee_user_id: Optional[UUID] = None
    ai_handoff_at: Optional[datetime] = None
    ai_first_response_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    closed_by_user_id: Optional[UUID] = None
    closed_by_role: Optional[ClosedByRole] = None
    title: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "requester_user_id": str(self.requester_user_id),
            "requester_org_id": str(self.requester_org_id),
            "category": self.category.value,
            "status": self.status.value,
            "assignee_user_id": str(self.assignee_user_id) if self.assignee_user_id else None,
            "ai_handoff_at": self.ai_handoff_at.isoformat() if self.ai_handoff_at else None,
            "ai_first_response_at": self.ai_first_response_at.isoformat() if self.ai_first_response_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "closed_by_user_id": str(self.closed_by_user_id) if self.closed_by_user_id else None,
            "closed_by_role": self.closed_by_role.value if self.closed_by_role else None,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SupportTicket":
        """Create from dictionary — accepts both wire-format strings and native types."""
        def _uuid_or_none(v):
            if v is None:
                return None
            return UUID(v) if isinstance(v, str) else v

        def _dt_or_none(v):
            if v is None:
                return None
            return datetime.fromisoformat(v) if isinstance(v, str) else v

        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            requester_user_id=_uuid_or_none(data["requester_user_id"]),
            requester_org_id=_uuid_or_none(data["requester_org_id"]),
            category=SupportTicketCategory(data["category"]),
            status=SupportTicketStatus(data["status"]),
            assignee_user_id=_uuid_or_none(data.get("assignee_user_id")),
            ai_handoff_at=_dt_or_none(data.get("ai_handoff_at")),
            ai_first_response_at=_dt_or_none(data.get("ai_first_response_at")),
            closed_at=_dt_or_none(data.get("closed_at")),
            closed_by_user_id=_uuid_or_none(data.get("closed_by_user_id")),
            closed_by_role=ClosedByRole(data["closed_by_role"]) if data.get("closed_by_role") else None,
            title=data.get("title"),
            created_at=_dt_or_none(data["created_at"]) if data.get("created_at") else datetime.utcnow(),
            updated_at=_dt_or_none(data["updated_at"]) if data.get("updated_at") else datetime.utcnow(),
        )
