"""
Users Service

Business logic for user management.
"""
import logging
import re
import bcrypt
from typing import Optional, List
from uuid import UUID

from ..models.user import User
from ..storage.user_storage import UserStorage

logger = logging.getLogger("rugpt.services.users")


class UsersService:
    """Service for user management"""

    def __init__(self, user_storage: UserStorage):
        self.user_storage = user_storage

    async def create_user(
        self,
        org_id: UUID,
        name: str,
        username: str,
        email: str,
        password: str,
        is_admin: bool = False,
        role_id: Optional[UUID] = None,
        avatar_url: Optional[str] = None
    ) -> User:
        """
        Create a new user.

        Args:
            org_id: Organization ID
            name: Display name
            username: Unique username within org
            email: Email address
            password: Plain text password (will be hashed)
            is_admin: Is organization admin
            role_id: Assigned AI role ID
            avatar_url: Profile picture URL

        Returns:
            Created user

        Raises:
            ValueError: If email or username already exists
        """
        # Normalize
        username = username.lower().strip()
        email = email.lower().strip()

        # Validate email format
        if not self._is_valid_email(email):
            raise ValueError(f"Invalid email format: {email}")

        # Validate username format
        if not self._is_valid_username(username):
            raise ValueError(f"Invalid username format: {username}")

        # Check if email exists
        if await self.user_storage.exists_by_email(email):
            raise ValueError(f"User with email '{email}' already exists")

        # Check if username exists in org
        if await self.user_storage.exists_by_username(username, org_id):
            raise ValueError(f"Username '{username}' already taken in this organization")

        # Hash password
        password_hash = self._hash_password(password)

        user = User(
            org_id=org_id,
            name=name,
            username=username,
            email=email,
            password_hash=password_hash,
            is_admin=is_admin,
            role_id=role_id,
            avatar_url=avatar_url
        )

        created_user = await self.user_storage.create(user)
        logger.info(f"Created user: {created_user.name} (@{created_user.username})")

        return created_user

    async def get_user(self, user_id: UUID) -> Optional[User]:
        """Get user by ID"""
        return await self.user_storage.get_by_id(user_id)

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email"""
        return await self.user_storage.get_by_email(email.lower())

    async def get_user_by_username(self, username: str, org_id: UUID) -> Optional[User]:
        """Get user by username within organization"""
        return await self.user_storage.get_by_username(username.lower(), org_id)

    async def list_users(self, org_id: UUID, active_only: bool = True) -> List[User]:
        """List users in organization"""
        return await self.user_storage.list_by_org(org_id, active_only)

    async def update_user(
        self,
        user_id: UUID,
        name: Optional[str] = None,
        username: Optional[str] = None,
        email: Optional[str] = None,
        avatar_url: Optional[str] = None,
        is_admin: Optional[bool] = None
    ) -> Optional[User]:
        """Update user profile"""
        user = await self.user_storage.get_by_id(user_id)
        if not user:
            return None

        if name:
            user.name = name

        if username:
            username = username.lower().strip()
            if not self._is_valid_username(username):
                raise ValueError(f"Invalid username format: {username}")
            if await self.user_storage.exists_by_username(username, user.org_id, exclude_id=user_id):
                raise ValueError(f"Username '{username}' already taken")
            user.username = username

        if email:
            email = email.lower().strip()
            if not self._is_valid_email(email):
                raise ValueError(f"Invalid email format: {email}")
            if await self.user_storage.exists_by_email(email, exclude_id=user_id):
                raise ValueError(f"Email '{email}' already in use")
            user.email = email

        if avatar_url is not None:
            user.avatar_url = avatar_url

        if is_admin is not None:
            user.is_admin = is_admin

        updated = await self.user_storage.update(user)
        logger.info(f"Updated user: {updated.name}")
        return updated

    async def change_password(self, user_id: UUID, new_password: str) -> bool:
        """Change user password"""
        user = await self.user_storage.get_by_id(user_id)
        if not user:
            return False

        user.password_hash = self._hash_password(new_password)
        await self.user_storage.update(user)
        logger.info(f"Changed password for user: {user.username}")
        return True

    async def verify_password(self, user_id: UUID, password: str) -> bool:
        """Verify user password"""
        user = await self.user_storage.get_by_id(user_id)
        if not user or not user.password_hash:
            return False
        return self._check_password(password, user.password_hash)

    async def assign_role(self, user_id: UUID, role_id: Optional[UUID]) -> bool:
        """Assign AI role to user"""
        result = await self.user_storage.assign_role(user_id, role_id)
        if result:
            action = f"assigned role {role_id}" if role_id else "unassigned role"
            logger.info(f"User {user_id}: {action}")
        return result

    async def deactivate_user(self, user_id: UUID) -> bool:
        """Deactivate (soft delete) user"""
        result = await self.user_storage.delete(user_id)
        if result:
            logger.info(f"Deactivated user: {user_id}")
        return result

    async def update_last_seen(self, user_id: UUID) -> None:
        """Update user's last seen timestamp"""
        await self.user_storage.update_last_seen(user_id)

    def _hash_password(self, password: str) -> str:
        """Hash password using bcrypt"""
        salt = bcrypt.gensalt(rounds=12)
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

    def _check_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))

    def _is_valid_email(self, email: str) -> bool:
        """Validate email format"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    def _is_valid_username(self, username: str) -> bool:
        """Validate username format (alphanumeric + underscores, 3-30 chars)"""
        pattern = r'^[a-z0-9_]{3,30}$'
        return bool(re.match(pattern, username))
