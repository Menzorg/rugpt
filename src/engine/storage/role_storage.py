"""
Role Storage

PostgreSQL storage for AI roles.
"""
import json
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.role import Role

logger = logging.getLogger("rugpt.storage.role")


class RoleStorage(BaseStorage):
    """Storage for Role entities"""

    async def create(self, role: Role) -> Role:
        """Create a new role"""
        query = """
            INSERT INTO roles (
                id, org_id, name, code, description, system_prompt,
                rag_collection, model_name, agent_type, agent_config,
                tools, prompt_file, is_active, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            role.id, role.org_id, role.name, role.code, role.description,
            role.system_prompt, role.rag_collection, role.model_name,
            role.agent_type, json.dumps(role.agent_config),
            json.dumps(role.tools), role.prompt_file,
            role.is_active, role.created_at, role.updated_at
        )
        return self._row_to_role(row)

    async def get_by_id(self, role_id: UUID) -> Optional[Role]:
        """Get role by ID"""
        query = "SELECT * FROM roles WHERE id = $1"
        row = await self.fetchrow(query, role_id)
        return self._row_to_role(row) if row else None

    async def get_by_code(self, code: str, org_id: UUID) -> Optional[Role]:
        """Get role by code within organization"""
        query = "SELECT * FROM roles WHERE code = $1 AND org_id = $2"
        row = await self.fetchrow(query, code.lower(), org_id)
        return self._row_to_role(row) if row else None

    async def list_by_org(self, org_id: UUID, active_only: bool = True) -> List[Role]:
        """List roles in organization"""
        if active_only:
            query = """
                SELECT * FROM roles
                WHERE org_id = $1 AND is_active = true
                ORDER BY name
            """
        else:
            query = "SELECT * FROM roles WHERE org_id = $1 ORDER BY name"
        rows = await self.fetch(query, org_id)
        return [self._row_to_role(row) for row in rows]

    async def update(self, role: Role) -> Role:
        """Update role"""
        role.updated_at = datetime.utcnow()
        query = """
            UPDATE roles
            SET name = $2, code = $3, description = $4, system_prompt = $5,
                rag_collection = $6, model_name = $7, agent_type = $8,
                agent_config = $9, tools = $10, prompt_file = $11,
                is_active = $12, updated_at = $13
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            role.id, role.name, role.code, role.description, role.system_prompt,
            role.rag_collection, role.model_name, role.agent_type,
            json.dumps(role.agent_config), json.dumps(role.tools),
            role.prompt_file, role.is_active, role.updated_at
        )
        return self._row_to_role(row)

    async def delete(self, role_id: UUID) -> bool:
        """Soft delete role"""
        query = """
            UPDATE roles
            SET is_active = false, updated_at = $2
            WHERE id = $1
        """
        result = await self.execute(query, role_id, datetime.utcnow())
        return "UPDATE 1" in result

    async def exists_by_code(self, code: str, org_id: UUID, exclude_id: Optional[UUID] = None) -> bool:
        """Check if role with code exists in organization"""
        if exclude_id:
            query = "SELECT 1 FROM roles WHERE code = $1 AND org_id = $2 AND id != $3"
            result = await self.fetchval(query, code.lower(), org_id, exclude_id)
        else:
            query = "SELECT 1 FROM roles WHERE code = $1 AND org_id = $2"
            result = await self.fetchval(query, code.lower(), org_id)
        return result is not None

    def _row_to_role(self, row) -> Role:
        """Convert database row to Role"""
        # agent_config and tools come as JSONB — asyncpg returns dicts/lists
        agent_config = row["agent_config"] if row["agent_config"] else {}
        tools = row["tools"] if row["tools"] else []
        # If asyncpg returned a string (shouldn't, but be safe), parse it
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
            system_prompt=row["system_prompt"],
            rag_collection=row["rag_collection"],
            model_name=row["model_name"],
            agent_type=row["agent_type"],
            agent_config=agent_config,
            tools=tools,
            prompt_file=row["prompt_file"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )
