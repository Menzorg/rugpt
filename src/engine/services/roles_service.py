"""
Roles Service

Business logic for AI role management.
"""
import logging
import re
from typing import Optional, List
from uuid import UUID

from ..models.role import Role
from ..storage.role_storage import RoleStorage
from ..storage.user_storage import UserStorage

logger = logging.getLogger("rugpt.services.roles")


class RolesService:
    """Service for AI role management"""

    def __init__(self, role_storage: RoleStorage, user_storage: UserStorage):
        self.role_storage = role_storage
        self.user_storage = user_storage

    async def create_role(
        self,
        org_id: UUID,
        name: str,
        code: str,
        system_prompt: str,
        description: Optional[str] = None,
        model_name: str = "qwen2.5:7b",
        rag_collection: Optional[str] = None
    ) -> Role:
        """
        Create a new AI role.

        Args:
            org_id: Organization ID
            name: Role display name (e.g., "Lawyer")
            code: Unique code (e.g., "lawyer")
            system_prompt: System prompt defining AI behavior
            description: Optional role description
            model_name: LLM model to use
            rag_collection: RAG collection name (for future)

        Returns:
            Created role

        Raises:
            ValueError: If code already exists in org
        """
        # Normalize code
        code = code.lower().strip()

        # Validate code format
        if not self._is_valid_code(code):
            raise ValueError(f"Invalid role code format: {code}")

        # Check if code exists in org
        if await self.role_storage.exists_by_code(code, org_id):
            raise ValueError(f"Role with code '{code}' already exists in this organization")

        role = Role(
            org_id=org_id,
            name=name,
            code=code,
            description=description,
            system_prompt=system_prompt,
            rag_collection=rag_collection,
            model_name=model_name
        )

        created = await self.role_storage.create(role)
        logger.info(f"Created role: {created.name} ({created.code})")
        return created

    async def get_role(self, role_id: UUID) -> Optional[Role]:
        """Get role by ID"""
        return await self.role_storage.get_by_id(role_id)

    async def get_role_by_code(self, code: str, org_id: UUID) -> Optional[Role]:
        """Get role by code within organization"""
        return await self.role_storage.get_by_code(code.lower(), org_id)

    async def list_roles(self, org_id: UUID, active_only: bool = True) -> List[Role]:
        """List roles in organization"""
        return await self.role_storage.list_by_org(org_id, active_only)

    async def update_role(
        self,
        role_id: UUID,
        name: Optional[str] = None,
        code: Optional[str] = None,
        description: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model_name: Optional[str] = None,
        rag_collection: Optional[str] = None
    ) -> Optional[Role]:
        """Update role"""
        role = await self.role_storage.get_by_id(role_id)
        if not role:
            return None

        if name:
            role.name = name

        if code:
            code = code.lower().strip()
            if not self._is_valid_code(code):
                raise ValueError(f"Invalid role code format: {code}")
            if await self.role_storage.exists_by_code(code, role.org_id, exclude_id=role_id):
                raise ValueError(f"Role with code '{code}' already exists")
            role.code = code

        if description is not None:
            role.description = description

        if system_prompt:
            role.system_prompt = system_prompt

        if model_name:
            role.model_name = model_name

        if rag_collection is not None:
            role.rag_collection = rag_collection

        updated = await self.role_storage.update(role)
        logger.info(f"Updated role: {updated.name}")
        return updated

    async def deactivate_role(self, role_id: UUID) -> bool:
        """Deactivate role"""
        # First, unassign role from all users
        users_with_role = await self.user_storage.list_by_role(role_id)
        for user in users_with_role:
            await self.user_storage.assign_role(user.id, None)
            logger.info(f"Unassigned role from user: {user.username}")

        result = await self.role_storage.delete(role_id)
        if result:
            logger.info(f"Deactivated role: {role_id}")
        return result

    async def get_users_with_role(self, role_id: UUID) -> List:
        """Get list of users assigned to this role"""
        return await self.user_storage.list_by_role(role_id)

    def _is_valid_code(self, code: str) -> bool:
        """Validate role code format (alphanumeric + underscores, 2-30 chars)"""
        pattern = r'^[a-z][a-z0-9_]{1,29}$'
        return bool(re.match(pattern, code))
