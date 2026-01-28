"""
Message Model

Represents a message in a chat, including mentions.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID, uuid4


class SenderType(str, Enum):
    """Type of message sender"""
    USER = "user"         # Human user
    AI_ROLE = "ai_role"   # AI responding as a role


class MentionType(str, Enum):
    """Type of mention in a message"""
    USER = "user"         # @ mention - reference to human user
    AI_ROLE = "ai_role"   # @@ mention - reference to user's AI role


@dataclass
class Mention:
    """
    Mention in a message.

    @ mention (USER): Notifies a human user
    @@ mention (AI_ROLE): Triggers AI response using the mentioned user's role
    """
    type: MentionType                    # @ or @@
    user_id: UUID = field(default_factory=uuid4)   # Referenced user ID
    username: str = ""                   # Username at time of mention
    position: int = 0                    # Position in message text

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            "type": self.type.value,
            "user_id": str(self.user_id),
            "username": self.username,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Mention":
        """Create from dictionary"""
        return cls(
            type=MentionType(data["type"]) if isinstance(data.get("type"), str) else data.get("type", MentionType.USER),
            user_id=UUID(data["user_id"]) if isinstance(data.get("user_id"), str) else data.get("user_id", uuid4()),
            username=data.get("username", ""),
            position=data.get("position", 0),
        )


@dataclass
class Message:
    """
    Message entity.

    A message can be sent by:
    - USER: Human user typing a message
    - AI_ROLE: AI responding as a user's role (triggered by @@ mention)

    For AI responses:
    - ai_validated: Whether the user has confirmed the AI response
    - reply_to_id: ID of message this is replying to (for @@ responses)
    """
    id: UUID = field(default_factory=uuid4)
    chat_id: UUID = field(default_factory=uuid4)    # Chat this message belongs to
    sender_type: SenderType = SenderType.USER        # Who sent this message
    sender_id: UUID = field(default_factory=uuid4)   # User ID (or role owner for AI)
    content: str = ""                                 # Message text
    mentions: List[Mention] = field(default_factory=list)  # Mentions in this message
    reply_to_id: Optional[UUID] = None               # Reply to message ID
    ai_validated: bool = False                        # AI response validated by user
    ai_edited: bool = False                           # AI response was edited by user
    is_deleted: bool = False                          # Soft delete flag
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "chat_id": str(self.chat_id),
            "sender_type": self.sender_type.value,
            "sender_id": str(self.sender_id),
            "content": self.content,
            "mentions": [m.to_dict() for m in self.mentions],
            "reply_to_id": str(self.reply_to_id) if self.reply_to_id else None,
            "ai_validated": self.ai_validated,
            "ai_edited": self.ai_edited,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """Create from dictionary"""
        mentions = data.get("mentions", [])
        if mentions and isinstance(mentions[0], dict):
            mentions = [Mention.from_dict(m) for m in mentions]

        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            chat_id=UUID(data["chat_id"]) if isinstance(data.get("chat_id"), str) else data.get("chat_id", uuid4()),
            sender_type=SenderType(data["sender_type"]) if isinstance(data.get("sender_type"), str) else data.get("sender_type", SenderType.USER),
            sender_id=UUID(data["sender_id"]) if isinstance(data.get("sender_id"), str) else data.get("sender_id", uuid4()),
            content=data.get("content", ""),
            mentions=mentions,
            reply_to_id=UUID(data["reply_to_id"]) if data.get("reply_to_id") and isinstance(data["reply_to_id"], str) else data.get("reply_to_id"),
            ai_validated=data.get("ai_validated", False),
            ai_edited=data.get("ai_edited", False),
            is_deleted=data.get("is_deleted", False),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
        )
