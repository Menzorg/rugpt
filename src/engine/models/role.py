"""
Role Model

Represents an AI role (agent) in the RuGPT system.
Roles are AI personas with specific system prompts and capabilities.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


@dataclass
class Role:
    """
    Role entity - represents an AI agent persona.

    Each role:
    - Belongs to an organization
    - Has a system prompt defining its behavior
    - Can have a dedicated RAG collection (for document-based knowledge)
    - Can use a specific LLM model

    Examples: Lawyer, Accountant, HR Manager, Tech Support, etc.

    When a user with this role is @@ mentioned:
    1. AI responds using this role's system prompt
    2. RAG search is performed on role's document collection
    3. User can validate/edit the AI response
    """
    id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)     # Organization this role belongs to
    name: str = ""                                   # Display name: "Lawyer"
    code: str = ""                                   # Unique code: "lawyer"
    description: Optional[str] = None                # Role description
    system_prompt: str = ""                          # System prompt for the AI
    rag_collection: Optional[str] = None             # RAG collection name (for future)
    model_name: str = "qwen2.5:7b"                   # LLM model to use
    is_active: bool = True                           # Active/inactive status
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert to dictionary for API response"""
        return {
            "id": str(self.id),
            "org_id": str(self.org_id),
            "name": self.name,
            "code": self.code,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "rag_collection": self.rag_collection,
            "model_name": self.model_name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Role":
        """Create from dictionary"""
        return cls(
            id=UUID(data["id"]) if isinstance(data.get("id"), str) else data.get("id", uuid4()),
            org_id=UUID(data["org_id"]) if isinstance(data.get("org_id"), str) else data.get("org_id", uuid4()),
            name=data.get("name", ""),
            code=data.get("code", ""),
            description=data.get("description"),
            system_prompt=data.get("system_prompt", ""),
            rag_collection=data.get("rag_collection"),
            model_name=data.get("model_name", "qwen2.5:7b"),
            is_active=data.get("is_active", True),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else data.get("created_at", datetime.utcnow()),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else data.get("updated_at", datetime.utcnow()),
        )
