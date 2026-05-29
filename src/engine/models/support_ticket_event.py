"""
SupportTicketEvent Model — audit trail entry for a support ticket.

Created by SupportTicketService on every meaningful state transition
(create / ai_responded / ai_handoff / taken / closed / reopened / message).
Append-only — events are never updated or deleted.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4


class SupportTicketEventType(str, Enum):
    """Support ticket event types — audit trail of ticket lifecycle."""
    CREATED = "created"
    AI_RESPONDED = "ai_responded"
    AI_HANDOFF = "ai_handoff"
    TAKEN = "taken"
    CLOSED = "closed"
    REOPENED = "reopened"
    MESSAGE = "message"


class SupportTicketActorRole(str, Enum):
    """Who triggered the event — match SQL CHECK in migration 023."""
    REQUESTER = "requester"
    OPERATOR = "operator"
    AI = "ai"
    SYSTEM = "system"


@dataclass
class SupportTicketEvent:
    """
    Audit trail entry for a support ticket.

    Append-only: created on every state transition, never updated/deleted.
    Cascades on ticket delete.
    """
    id: UUID = field(default_factory=uuid4)
    ticket_id: UUID = field(default_factory=uuid4)
    actor_user_id: UUID = field(default_factory=uuid4)
    actor_role: SupportTicketActorRole = SupportTicketActorRole.SYSTEM
    event_type: SupportTicketEventType = SupportTicketEventType.CREATED
    payload: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "ticket_id": str(self.ticket_id),
            "actor_user_id": str(self.actor_user_id),
            "actor_role": self.actor_role.value,
            "event_type": self.event_type.value,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SupportTicketEvent":
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
            ticket_id=_uuid_or_none(data["ticket_id"]),
            actor_user_id=_uuid_or_none(data["actor_user_id"]),
            actor_role=SupportTicketActorRole(data["actor_role"]),
            event_type=SupportTicketEventType(data["event_type"]),
            payload=data.get("payload") or {},
            created_at=_dt_or_none(data["created_at"]) if data.get("created_at") else datetime.utcnow(),
        )
