"""
Roles Service

Business logic for AI role management.
Roles are predefined (created via migrations/seeds), not via API.
Admin can only view roles and assign them to users.
"""
import logging
from typing import Optional, List
from uuid import UUID

from ..models.role import Role
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage
from .prompt_cache import PromptCache

logger = logging.getLogger("rugpt.services.roles")


class RolesService:
    """Service for AI role management (read-only + prompt cache)"""

    def __init__(
        self,
        role_storage: RoleStorage,
        user_storage: UserStorage,
        prompt_cache: Optional[PromptCache] = None,
    ):
        self.role_storage = role_storage
        self.user_storage = user_storage
        self.prompt_cache = prompt_cache

    async def get_role(self, role_id: UUID) -> Optional[Role]:
        """Get role by ID"""
        return await self.role_storage.get_by_id(role_id)

    async def get_role_by_code(self, code: str, org_id: UUID) -> Optional[Role]:
        """Get role by code within organization"""
        return await self.role_storage.get_by_code(code.lower(), org_id)

    async def list_roles(self, org_id: UUID, active_only: bool = True) -> List[Role]:
        """List roles in organization"""
        return await self.role_storage.list_by_org(org_id, active_only)

    async def get_users_with_role(self, role_id: UUID) -> List:
        """Get list of users assigned to this role"""
        return await self.user_storage.list_by_role(role_id)

    def get_system_prompt(self, role: Role) -> str:
        """
        Get system prompt for a role.
        Uses PromptCache if available (reads from file, caches in memory).
        Falls back to system_prompt from DB.
        """
        if self.prompt_cache:
            return self.prompt_cache.get_prompt(role)
        return role.system_prompt or ""

    def clear_prompt_cache(self, prompt_file: Optional[str] = None):
        """Clear prompt cache (all or specific file)"""
        if self.prompt_cache:
            self.prompt_cache.clear(prompt_file)
