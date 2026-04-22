"""
Chat Storage

PostgreSQL storage for chats.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.chat import Chat, ChatType, _coerce_chat_type

logger = logging.getLogger("rugpt.storage.chat")


class ChatStorage(BaseStorage):
    """Storage for Chat entities"""

    async def create(self, chat: Chat) -> Chat:
        """Create a new chat"""
        query = """
            INSERT INTO chats (
                id, org_id, type, name, participants, created_by,
                task_id, project_id,
                is_active, created_at, updated_at, last_message_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            chat.id, chat.org_id, chat.type.value, chat.name,
            [str(p) for p in chat.participants], chat.created_by,
            chat.task_id, chat.project_id,
            chat.is_active, chat.created_at, chat.updated_at, chat.last_message_at
        )
        return self._row_to_chat(row)

    async def get_by_id(self, chat_id: UUID) -> Optional[Chat]:
        """Get chat by ID"""
        query = "SELECT * FROM chats WHERE id = $1"
        row = await self.fetchrow(query, chat_id)
        return self._row_to_chat(row) if row else None

    async def get_direct_chat(self, user1_id: UUID, user2_id: UUID) -> Optional[Chat]:
        """Get direct chat between two users"""
        query = """
            SELECT * FROM chats
            WHERE type = 'direct'
              AND $1 = ANY(participants)
              AND $2 = ANY(participants)
              AND array_length(participants, 1) = 2
            LIMIT 1
        """
        row = await self.fetchrow(query, str(user1_id), str(user2_id))
        return self._row_to_chat(row) if row else None

    async def list_by_user(
        self,
        user_id: UUID,
        active_only: bool = True,
        chat_type: Optional[str] = None,
    ) -> List[Chat]:
        """List chats for a user, optionally filtered by type."""
        conditions = ["$1 = ANY(participants)"]
        params: List = [str(user_id)]
        if active_only:
            conditions.append("is_active = true")
        if chat_type is not None:
            params.append(chat_type)
            conditions.append(f"type = ${len(params)}")
        query = f"""
            SELECT * FROM chats
            WHERE {' AND '.join(conditions)}
            ORDER BY last_message_at DESC NULLS LAST, created_at DESC
        """
        rows = await self.fetch(query, *params)
        return [self._row_to_chat(row) for row in rows]

    async def get_by_task_id(self, task_id: UUID) -> Optional[Chat]:
        """Get chat associated with a task (if any)."""
        row = await self.fetchrow(
            "SELECT * FROM chats WHERE task_id = $1 LIMIT 1", task_id,
        )
        return self._row_to_chat(row) if row else None

    async def get_by_project_id(self, project_id: UUID) -> Optional[Chat]:
        """Get chat associated with a project (if any)."""
        row = await self.fetchrow(
            "SELECT * FROM chats WHERE project_id = $1 LIMIT 1", project_id,
        )
        return self._row_to_chat(row) if row else None

    async def list_by_org(self, org_id: UUID, active_only: bool = True) -> List[Chat]:
        """List all chats in organization"""
        if active_only:
            query = """
                SELECT * FROM chats
                WHERE org_id = $1 AND is_active = true
                ORDER BY last_message_at DESC NULLS LAST
            """
        else:
            query = "SELECT * FROM chats WHERE org_id = $1 ORDER BY last_message_at DESC NULLS LAST"
        rows = await self.fetch(query, org_id)
        return [self._row_to_chat(row) for row in rows]

    async def update(self, chat: Chat) -> Chat:
        """Update chat"""
        chat.updated_at = datetime.utcnow()
        query = """
            UPDATE chats
            SET name = $2, participants = $3, is_active = $4,
                updated_at = $5, last_message_at = $6
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            chat.id, chat.name, [str(p) for p in chat.participants],
            chat.is_active, chat.updated_at, chat.last_message_at
        )
        return self._row_to_chat(row)

    async def update_last_message(self, chat_id: UUID) -> None:
        """Update last message timestamp"""
        query = "UPDATE chats SET last_message_at = $2, updated_at = $2 WHERE id = $1"
        await self.execute(query, chat_id, datetime.utcnow())

    async def update_mem_id(self, chat_id: UUID, mem_id: UUID) -> None:
        """Set the active memory snapshot for a chat"""
        await self.execute(
            "UPDATE chats SET mem_id = $2, updated_at = $3 WHERE id = $1",
            chat_id, mem_id, datetime.utcnow(),
        )

    async def add_participant(self, chat_id: UUID, user_id: UUID) -> bool:
        """Add participant to chat"""
        query = """
            UPDATE chats
            SET participants = array_append(participants, $2), updated_at = $3
            WHERE id = $1 AND NOT $2 = ANY(participants)
        """
        result = await self.execute(query, chat_id, str(user_id), datetime.utcnow())
        return "UPDATE 1" in result

    async def remove_participant(self, chat_id: UUID, user_id: UUID) -> bool:
        """Remove participant from chat"""
        query = """
            UPDATE chats
            SET participants = array_remove(participants, $2), updated_at = $3
            WHERE id = $1
        """
        result = await self.execute(query, chat_id, str(user_id), datetime.utcnow())
        return "UPDATE 1" in result

    async def delete(self, chat_id: UUID) -> bool:
        """Soft delete chat (archive)"""
        query = "UPDATE chats SET is_active = false, updated_at = $2 WHERE id = $1"
        result = await self.execute(query, chat_id, datetime.utcnow())
        return "UPDATE 1" in result

    def _row_to_chat(self, row) -> Chat:
        """Convert database row to Chat"""
        participants = row["participants"] or []
        if participants and isinstance(participants[0], str):
            participants = [UUID(p) for p in participants]

        keys = set(row.keys())
        task_id = row["task_id"] if "task_id" in keys else None
        project_id = row["project_id"] if "project_id" in keys else None
        mem_id = row["mem_id"] if "mem_id" in keys else None

        return Chat(
            id=row["id"],
            org_id=row["org_id"],
            type=_coerce_chat_type(row["type"]),
            name=row["name"],
            participants=participants,
            created_by=row["created_by"],
            task_id=task_id,
            project_id=project_id,
            mem_id=mem_id,
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_message_at=row["last_message_at"]
        )
