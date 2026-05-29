"""
Role Subagent Storage

Read storage for supervisor -> subagent role mappings.
"""
import json
import logging
from typing import List
from uuid import UUID

from .base import BaseStorage
from ..models.role import Role

logger = logging.getLogger("rugpt.storage.role_subagent")


class RoleSubagentStorage(BaseStorage):
    """Storage for role_subagents mappings."""

    async def list_available_subagent_roles(self, role_id: UUID) -> List[Role]:
        """List active roles explicitly linked as subagents for role_id."""
        rows = await self.fetch(
            """
            SELECT r.*
            FROM role_subagents rs
            JOIN roles r ON r.id = rs.subagent_role_id
            WHERE rs.role_id = $1 AND r.is_active = true
            ORDER BY r.name, r.code
            """,
            role_id,
        )
        return [self._row_to_role(row) for row in rows]

    def _row_to_role(self, row) -> Role:
        """Convert database row to Role."""
        agent_config = row["agent_config"] if row["agent_config"] else {}
        tools = row["tools"] if row["tools"] else []
        if isinstance(agent_config, str):
            agent_config = json.loads(agent_config)
        if isinstance(tools, str):
            tools = json.loads(tools)

        return Role(
            id=row["id"],
            org_id=row["org_id"],
            name=row["name"],
            code=row["code"],
            description=row["description"],
            agent_scope_description=row["agent_scope_description"] or "",
            system_prompt=row["system_prompt"],
            rag_collection=row["rag_collection"],
            model_name=row["model_name"],
            agent_type=row["agent_type"],
            agent_config=agent_config,
            tools=tools,
            prompt_file=row["prompt_file"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
