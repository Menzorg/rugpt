"""
Chat Model

Represents a chat/conversation in the RuGPT system.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID, uuid4


class ChatType(str, Enum):
    """Types of chats in the system"""
    DIRECT = "direct"     # Direct message between two users (including user <-> system user)
    TASK = "task"         # Chat attached to a task
    PROJECT = "project"   # Chat attached to a project
    SUPPORT = "support"   # Tech support ticket chat (cross-org, see ChatType.SUPPORT exemption)


def _coerce_chat_type(raw) -> ChatType:
    """Coerce legacy chat type strings ('main', 'group') to DIRECT.

    Migration 017 normalises DB rows, but this fallback protects Chat.from_dict
    against stale callers or partially migrated fixtures.
    """
    if isinstance(raw, ChatType):
        return raw
    if isinstance(raw, str):
        if raw in ("main", "group"):
            return ChatType.DIRECT
        try:
            return ChatType(raw)
        except ValueError:
            return ChatType.DIRECT
    return ChatType.DIRECT


@dataclass
class Chat:
    """
    Chat entity - represents a conversation.

    Chat types:
    1. DIRECT - Chat between users (or user <-> system user). 1..N participants.
    2. TASK - Chat attached to a task (task_id set). Participants = creator + assignee.
    3. PROJECT - Chat attached to a project (project_id set). Participants = union of task creators/assignees in the project.
    4. SUPPORT - Chat attached to a support ticket (support_ticket_id set). Cross-org: chat.org_id = requester_org_id, but operator from RuGPT Support org joins as participant. Visibility uses ChatService.can_user_access_chat exemption.

    Participants: List of user IDs in this chat. Monotonic-grow (audit trail).
    Visibility in sidebar/UI is separately enforced via can_see_task etc.
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)     # Organization this chat belongs to
    type: ChatType = ChatType.DIRECT                 # Chat type
    name: Optional[str] = None                       # Chat name (legacy, used by direct groupings)
    participants: List[UUID] = field(default_factory=list)  # User IDs
    created_by: Optional[UUID] = None                # User who created the chat
    task_id: Optional[UUID] = None                   # Set iff type == TASK
    project_id: Optional[UUID] = None                # Set iff type == PROJECT
    support_ticket_id: Optional[UUID] = None         # Set iff type == SUPPORT
    is_active: bool = True                           # Active/archived status
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_message_at: Optional[datetime] = None       # Timestamp of last message

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "type": self.type.value,
            "name": self.name,
            "participants": [str(p) for p in self.participants],
            "created_by": str(self.created_by) if self.created_by else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "support_ticket_id": str(self.support_ticket_id) if self.support_ticket_id else None,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Chat":
        """Create from dictionary"""
        participants = data.get("participants", [])
        if participants and isinstance(participants[0], str):
            participants = [UUID(p) for p in participants]

        def _uuid_or_none(v):
            if v is None:
                return None
            return UUID(v) if isinstance(v, str) else v

        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            org_id=UUID(data["org_id"]) if isinstance(data.get("org_id"), str) else data.get("org_id", uuid4()),
            type=_coerce_chat_type(data.get("type")),
            name=data.get("name"),
            participants=participants,
            created_by=_uuid_or_none(data.get("created_by")),
            task_id=_uuid_or_none(data.get("task_id")),
            project_id=_uuid_or_none(data.get("project_id")),
            support_ticket_id=_uuid_or_none(data.get("support_ticket_id")),
            is_active=data.get("is_active", True),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
            last_message_at=datetime.fromisoformat(data["last_message_at"]) if data.get("last_message_at") and isinstance(data["last_message_at"], str) else data.get("last_message_at"),
        )
