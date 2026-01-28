"""
Message Storage

PostgreSQL storage for messages.
"""
import json
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from .base import BaseStorage
from ..models.message import Message, Mention, SenderType, MentionType

logger = logging.getLogger("rugpt.storage.message")


class MessageStorage(BaseStorage):
    """Storage for Message entities"""

    async def create(self, message: Message) -> Message:
        """Create a new message"""
        query = """
            INSERT INTO messages (
                id, chat_id, sender_type, sender_id, content, mentions,
                reply_to_id, ai_validated, ai_edited, is_deleted,
                created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING *
        """
        mentions_json = json.dumps([m.to_dict() for m in message.mentions])
        row = await self.fetchrow(
            query,
            message.id, message.chat_id, message.sender_type.value,
            message.sender_id, message.content, mentions_json,
            message.reply_to_id, message.ai_validated, message.ai_edited,
            message.is_deleted, message.created_at, message.updated_at
        )
        return self._row_to_message(row)

    async def get_by_id(self, message_id: UUID) -> Optional[Message]:
        """Get message by ID"""
        query = "SELECT * FROM messages WHERE id = $1 AND is_deleted = false"
        row = await self.fetchrow(query, message_id)
        return self._row_to_message(row) if row else None

    async def list_by_chat(
        self,
        chat_id: UUID,
        limit: int = 50,
        before_id: Optional[UUID] = None
    ) -> List[Message]:
        """List messages in a chat with pagination"""
        if before_id:
            query = """
                SELECT * FROM messages
                WHERE chat_id = $1 AND is_deleted = false AND created_at < (
                    SELECT created_at FROM messages WHERE id = $2
                )
                ORDER BY created_at DESC
                LIMIT $3
            """
            rows = await self.fetch(query, chat_id, before_id, limit)
        else:
            query = """
                SELECT * FROM messages
                WHERE chat_id = $1 AND is_deleted = false
                ORDER BY created_at DESC
                LIMIT $2
            """
            rows = await self.fetch(query, chat_id, limit)
        return [self._row_to_message(row) for row in reversed(rows)]

    async def list_unvalidated(self, user_id: UUID) -> List[Message]:
        """List AI messages that need validation by user"""
        query = """
            SELECT * FROM messages
            WHERE sender_type = 'ai_role'
              AND sender_id = $1
              AND ai_validated = false
              AND is_deleted = false
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, user_id)
        return [self._row_to_message(row) for row in rows]

    async def update(self, message: Message) -> Message:
        """Update message"""
        message.updated_at = datetime.utcnow()
        mentions_json = json.dumps([m.to_dict() for m in message.mentions])
        query = """
            UPDATE messages
            SET content = $2, mentions = $3, ai_validated = $4,
                ai_edited = $5, updated_at = $6
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            message.id, message.content, mentions_json,
            message.ai_validated, message.ai_edited, message.updated_at
        )
        return self._row_to_message(row)

    async def validate(self, message_id: UUID, edited_content: Optional[str] = None) -> Optional[Message]:
        """Validate AI message (optionally with edited content)"""
        now = datetime.utcnow()
        if edited_content:
            query = """
                UPDATE messages
                SET ai_validated = true, ai_edited = true,
                    content = $2, updated_at = $3
                WHERE id = $1 AND sender_type = 'ai_role'
                RETURNING *
            """
            row = await self.fetchrow(query, message_id, edited_content, now)
        else:
            query = """
                UPDATE messages
                SET ai_validated = true, updated_at = $2
                WHERE id = $1 AND sender_type = 'ai_role'
                RETURNING *
            """
            row = await self.fetchrow(query, message_id, now)
        return self._row_to_message(row) if row else None

    async def delete(self, message_id: UUID) -> bool:
        """Soft delete message"""
        query = "UPDATE messages SET is_deleted = true, updated_at = $2 WHERE id = $1"
        result = await self.execute(query, message_id, datetime.utcnow())
        return "UPDATE 1" in result

    async def count_by_chat(self, chat_id: UUID) -> int:
        """Count messages in chat"""
        query = "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND is_deleted = false"
        row = await self.fetchrow(query, chat_id)
        return row["count"] if row else 0

    def _row_to_message(self, row) -> Message:
        """Convert database row to Message"""
        mentions_data = row["mentions"]
        if isinstance(mentions_data, str):
            mentions_data = json.loads(mentions_data)
        mentions = [Mention.from_dict(m) for m in (mentions_data or [])]

        return Message(
            id=row["id"],
            chat_id=row["chat_id"],
            sender_type=SenderType(row["sender_type"]),
            sender_id=row["sender_id"],
            content=row["content"],
            mentions=mentions,
            reply_to_id=row["reply_to_id"],
            ai_validated=row["ai_validated"],
            ai_edited=row["ai_edited"],
            is_deleted=row["is_deleted"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
