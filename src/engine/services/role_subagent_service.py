"""
Role Subagent Service

Business logic for supervisor role delegation configuration.
"""
import logging
from typing import List
from uuid import UUID

from ..models.role import Role
from ..storage.role_subagent_storage import RoleSubagentStorage

logger = logging.getLogger("rugpt.services.role_subagent")


class RoleSubagentService:
    """Service for reading supervisor subagent role mappings."""

    def __init__(self, role_subagent_storage: RoleSubagentStorage):
        self.role_subagent_storage = role_subagent_storage

    async def get_available_subagent_roles(self, role_id: UUID) -> List[Role]:
        """Return active roles that role_id may call as subagents."""
        return await self.role_subagent_storage.list_available_subagent_roles(role_id)
