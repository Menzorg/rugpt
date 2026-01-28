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
    MAIN = "main"         # User's personal chat with their AI role
    DIRECT = "direct"     # Direct message between two users
    GROUP = "group"       # Group chat (multiple users)


@dataclass
class Chat:
    """
    Chat entity - represents a conversation.

    Chat types:
    1. MAIN - Each user has their own main chat with their AI role
       - Created automatically when user is created
       - User + AI role as participants
    2. DIRECT - Direct messages between two users
       - Both users can see the conversation
       - Can use @ and @@ mentions
    3. GROUP - Group conversation (optional, for future)
       - Multiple users
       - Can use @ and @@ mentions

    Participants: List of user IDs in this chat
    For MAIN chat: [user_id] (AI role is implied via user.role_id)
    For DIRECT: [user1_id, user2_id]
    For GROUP: [user1_id, user2_id, ..., userN_id]
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)     # Organization this chat belongs to
    type: ChatType = ChatType.MAIN                   # Chat type
    name: Optional[str] = None                       # Chat name (for groups)
    participants: List[UUID] = field(default_factory=list)  # User IDs
    created_by: Optional[UUID] = None                # User who created the chat
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

        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            org_id=UUID(data["org_id"]) if isinstance(data.get("org_id"), str) else data.get("org_id", uuid4()),
            type=ChatType(data["type"]) if isinstance(data.get("type"), str) else data.get("type", ChatType.MAIN),
            name=data.get("name"),
            participants=participants,
            created_by=UUID(data["created_by"]) if data.get("created_by") and isinstance(data["created_by"], str) else data.get("created_by"),
            is_active=data.get("is_active", True),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
            last_message_at=datetime.fromisoformat(data["last_message_at"]) if data.get("last_message_at") and isinstance(data["last_message_at"], str) else data.get("last_message_at"),
        )
