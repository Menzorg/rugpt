"""
User Model

Represents a user in the RuGPT system.
Users belong to an organization and can have an AI role assigned.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class User:
    """
    User entity.

    Each user:
    - Belongs to an organization (org_id)
    - Has access to the system
    - Has their own "main chat" with their AI role
    - Can have an AI role assigned (role_id)
    - Can be an admin (is_admin) - admins can manage org settings

    @ mention: Reference to this user (human responds)
    @@ mention: Reference to this user's AI role (AI responds, user validates)
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)     # Organization this user belongs to
    name: str = ""                                   # Display name: "Roman Petrovich"
    username: str = ""                               # Unique username: "roman_petrovich"
    email: str = ""                                  # Email address
    password_hash: Optional[str] = None              # Hashed password
    role_id: Optional[UUID] = None                   # AI role assigned to this user
    is_admin: bool = False                           # Is organization admin
    is_active: bool = True                           # Active/inactive status
    avatar_url: Optional[str] = None                 # Profile picture URL
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_seen_at: Optional[datetime] = None          # Last activity timestamp

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """Convert to dictionary for API response"""
        result = {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "name": self.name,
            "username": self.username,
            "email": self.email,
            "role_id": str(self.role_id) if self.role_id else None,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "avatar_url": self.avatar_url,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }
        if include_sensitive:
            result["password_hash"] = self.password_hash
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        """Create from dictionary"""
        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            org_id=UUID(data["org_id"]) if isinstance(data.get("org_id"), str) else data.get("org_id", uuid4()),
            name=data.get("name", ""),
            username=data.get("username", ""),
            email=data.get("email", ""),
            password_hash=data.get("password_hash"),
            role_id=UUID(data["role_id"]) if data.get("role_id") and isinstance(data["role_id"], str) else data.get("role_id"),
            is_admin=data.get("is_admin", False),
            is_active=data.get("is_active", True),
            avatar_url=data.get("avatar_url"),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
            last_seen_at=datetime.fromisoformat(data["last_seen_at"]) if data.get("last_seen_at") and isinstance(data["last_seen_at"], str) else data.get("last_seen_at"),
        )
