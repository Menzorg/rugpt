"""
User Storage

PostgreSQL storage for users.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.user import User

logger = logging.getLogger("rugpt.storage.user")


class UserStorage(BaseStorage):
    """Storage for User entities"""

    async def create(self, user: User) -> User:
        """Create a new user"""
        query = """
            INSERT INTO users (
                id, org_id, name, username, email, password_hash, role_id,
                is_admin, is_system, is_active, avatar_url, created_at, updated_at, last_seen_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            user.id, user.org_id, user.name, user.username, user.email,
            user.password_hash, user.role_id, user.is_admin, user.is_system, user.is_active,
            user.avatar_url, user.created_at, user.updated_at, user.last_seen_at
        )
        return self._row_to_user(row)

    async def get_by_id(self, user_id: UUID) -> Optional[User]:
        """Get user by ID"""
        query = "SELECT * FROM users WHERE id = $1"
        row = await self.fetchrow(query, user_id)
        return self._row_to_user(row) if row else None

    async def get_by_email(self, email: str) -> Optional[User]:
        """Get user by email"""
        query = "SELECT * FROM users WHERE email = $1"
        row = await self.fetchrow(query, email.lower())
        return self._row_to_user(row) if row else None

    async def get_by_username(self, username: str, org_id: UUID) -> Optional[User]:
        """Get user by username within organization"""
        query = "SELECT * FROM users WHERE username = $1 AND org_id = $2"
        row = await self.fetchrow(query, username.lower(), org_id)
        return self._row_to_user(row) if row else None

    async def list_by_org(self, org_id: UUID, active_only: bool = True) -> List[User]:
        """List users in organization"""
        if active_only:
            query = """
                SELECT * FROM users
                WHERE org_id = $1 AND is_active = true
                ORDER BY name
            """
        else:
            query = "SELECT * FROM users WHERE org_id = $1 ORDER BY name"
        rows = await self.fetch(query, org_id)
        return [self._row_to_user(row) for row in rows]

    async def list_by_role(self, role_id: UUID) -> List[User]:
        """List users assigned to a role"""
        query = """
            SELECT * FROM users
            WHERE role_id = $1 AND is_active = true
            ORDER BY name
        """
        rows = await self.fetch(query, role_id)
        return [self._row_to_user(row) for row in rows]

    async def get_system_users(self) -> List[User]:
        """
        Get all system users (AI assistants for admins).
        System users belong to RuGPT organization and are shared across all orgs.
        Returns list of system users (one per AI model: GPT-4, Qwen, Claude)
        """
        query = """
            SELECT * FROM users
            WHERE is_system = true AND is_active = true
            ORDER BY name
        """
        rows = await self.fetch(query)
        return [self._row_to_user(row) for row in rows]

    async def get_system_user_by_username(self, username: str) -> Optional[User]:
        """
        Get system user by username (regardless of org).
        Used to resolve @@mirror, @@ai_gpt4, etc. from any organization.
        """
        query = """
            SELECT * FROM users
            WHERE username = $1 AND is_system = true AND is_active = true
            LIMIT 1
        """
        row = await self.fetchrow(query, username.lower())
        return self._row_to_user(row) if row else None

    async def get_system_user_by_model(self, model_code: str) -> Optional[User]:
        """
        Get system user by model code (gpt4, qwen, claude).
        Example: model_code='gpt4' returns 'AI GPT-4' user
        """
        query = """
            SELECT u.* FROM users u
            JOIN roles r ON u.role_id = r.id
            WHERE u.is_system = true
              AND u.is_active = true
              AND r.code = $1
            LIMIT 1
        """
        row = await self.fetchrow(query, f'admin_{model_code}')
        return self._row_to_user(row) if row else None

    async def update(self, user: User) -> User:
        """Update user"""
        user.updated_at = datetime.utcnow()
        query = """
            UPDATE users
            SET name = $2, username = $3, email = $4, password_hash = $5,
                role_id = $6, is_admin = $7, is_system = $8, is_active = $9, avatar_url = $10,
                updated_at = $11, last_seen_at = $12
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            user.id, user.name, user.username, user.email, user.password_hash,
            user.role_id, user.is_admin, user.is_system, user.is_active, user.avatar_url,
            user.updated_at, user.last_seen_at
        )
        return self._row_to_user(row)

    async def update_last_seen(self, user_id: UUID) -> None:
        """Update user's last seen timestamp"""
        query = "UPDATE users SET last_seen_at = $2 WHERE id = $1"
        await self.execute(query, user_id, datetime.utcnow())

    async def assign_role(self, user_id: UUID, role_id: Optional[UUID]) -> bool:
        """Assign or unassign role to user"""
        query = "UPDATE users SET role_id = $2, updated_at = $3 WHERE id = $1"
        result = await self.execute(query, user_id, role_id, datetime.utcnow())
        return "UPDATE 1" in result

    async def delete(self, user_id: UUID) -> bool:
        """Soft delete user"""
        query = """
            UPDATE users
            SET is_active = false, updated_at = $2
            WHERE id = $1
        """
        result = await self.execute(query, user_id, datetime.utcnow())
        return "UPDATE 1" in result

    async def exists_by_email(self, email: str, exclude_id: Optional[UUID] = None) -> bool:
        """Check if user with email exists"""
        if exclude_id:
            query = "SELECT 1 FROM users WHERE email = $1 AND id != $2"
            result = await self.fetchval(query, email.lower(), exclude_id)
        else:
            query = "SELECT 1 FROM users WHERE email = $1"
            result = await self.fetchval(query, email.lower())
        return result is not None

    async def exists_by_username(self, username: str, org_id: UUID, exclude_id: Optional[UUID] = None) -> bool:
        """Check if username exists in organization"""
        if exclude_id:
            query = "SELECT 1 FROM users WHERE username = $1 AND org_id = $2 AND id != $3"
            result = await self.fetchval(query, username.lower(), org_id, exclude_id)
        else:
            query = "SELECT 1 FROM users WHERE username = $1 AND org_id = $2"
            result = await self.fetchval(query, username.lower(), org_id)
        return result is not None

    def _row_to_user(self, row) -> User:
        """Convert database row to User"""
        return User(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            username=row["username"],
            email=row["email"],
            password_hash=row["password_hash"],
            role_id=row["role_id"],
            is_admin=row["is_admin"],
            is_system=row.get("is_system", False),  # Default False for backward compatibility
            is_active=row["is_active"],
            avatar_url=row["avatar_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"]
        )
