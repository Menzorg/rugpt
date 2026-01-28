"""
Organization Model

Represents an organization (tenant) in the multi-tenant RuGPT system.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Organization:
    """
    Organization entity - represents a company/tenant.

    Multi-tenancy: Each organization has isolated data.
    Users, roles, chats, and documents are scoped to an organization.
    """
    id: UUID = field(default_factory=uuid4)
    name: str = ""                          # "Acme Corp"
    slug: str = ""                          # "acme-corp" (URL-safe identifier)
    description: Optional[str] = None       # Optional description
    is_active: bool = True                  # Active/inactive status
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Organization":
        """Create from dictionary"""
        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            description=data.get("description"),
            is_active=data.get("is_active", True),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
        )
