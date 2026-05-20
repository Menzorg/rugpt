"""
Message Storage

PostgreSQL storage for messages.
"""
import json

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID

from .base import BaseStorage
from ..models.message import Message, Mention, SenderType, MentionType

if TYPE_CHECKING:
    from .message_attachment_storage import MessageAttachmentStorage

logger = get_logger("storage")

class MessageStorage(BaseStorage):
    """Storage for Message entities"""

    def __init__(self, postgres_dsn: str = "postgresql://postgres@localhost/rugpt"):
        super().__init__(postgres_dsn)
        # Optional dependency wired by EngineService after construction.
        # Used to bulk-hydrate attachments when reading messages.
        self.attachment_storage: Optional["MessageAttachmentStorage"] = None

    async def _hydrate_attachments(self, msgs: List[Message]) -> None:
        """Bulk-fetch attachments for `msgs` and assign to `m.attachments`.

        No-op if attachment_storage isn't wired (tests/legacy paths) or list is empty.
        """
        if not msgs or self.attachment_storage is None:
            return
        atts_by_msg = await self.attachment_storage.get_for_messages([m.id for m in msgs])
        for m in msgs:
            m.attachments = atts_by_msg.get(m.id, [])

    async def create(self, message: Message) -> Message:
        """Create a new message"""
        query = """
            INSERT INTO messages (
                id, chat_id, sender_type, sender_id, content, mentions,
                reply_to_id, ai_is_valid, ai_edited, is_deleted,
                metadata,
                created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING *
        """
        mentions_json = json.dumps([m.to_dict() for m in message.mentions])
        metadata_json = json.dumps(message.metadata or {})
        row = await self.fetchrow(
            query,
            message.id, message.chat_id, message.sender_type.value,
            message.sender_id, message.content, mentions_json,
            message.reply_to_id, message.ai_is_valid, message.ai_edited,
            message.is_deleted, metadata_json,
            message.created_at, message.updated_at
        )
        return self._row_to_message(row)

    async def get_by_id(self, message_id: UUID) -> Optional[Message]:
        """Get message by ID"""
        query = "SELECT * FROM messages WHERE id = $1 AND is_deleted = false"
        row = await self.fetchrow(query, message_id)
        if row is None:
            return None
        m = self._row_to_message(row)
        if self.attachment_storage is not None:
            m.attachments = await self.attachment_storage.get_for_message(m.id)
        return m

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
        msgs = [self._row_to_message(row) for row in reversed(rows)]
        await self._hydrate_attachments(msgs)
        return msgs

    async def list_pending_review(self, user_id: UUID) -> List[Message]:
        """List AI messages pending review by user (ai_is_valid IS NULL)"""
        query = """
            SELECT * FROM messages
            WHERE sender_type = 'ai_role'
              AND sender_id = $1
              AND ai_is_valid IS NULL
              AND is_deleted = false
            ORDER BY created_at DESC
        """
        rows = await self.fetch(query, user_id)
        return [self._row_to_message(row) for row in rows]

    async def list_reviewed(self, user_id: UUID, limit: int = 50) -> List[Message]:
        """List AI messages already validated/rejected by user (ai_is_valid IS NOT NULL).

        Парный к list_pending_review для UI таба «Моя роль → Проверенные».
        """
        query = """
            SELECT * FROM messages
            WHERE sender_type = 'ai_role'
              AND sender_id = $1
              AND ai_is_valid IS NOT NULL
              AND is_deleted = false
            ORDER BY updated_at DESC
            LIMIT $2
        """
        rows = await self.fetch(query, user_id, limit)
        return [self._row_to_message(row) for row in rows]

    async def update(self, message: Message) -> Message:
        """Update message"""
        message.updated_at = datetime.utcnow()
        mentions_json = json.dumps([m.to_dict() for m in message.mentions])
        query = """
            UPDATE messages
            SET content = $2, mentions = $3, ai_is_valid = $4,
                ai_edited = $5, updated_at = $6
            WHERE id = $1
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            message.id, message.content, mentions_json,
            message.ai_is_valid, message.ai_edited, message.updated_at
        )
        return self._row_to_message(row)

    async def validate(self, message_id: UUID, edited_content: Optional[str] = None) -> Optional[Message]:
        """Validate AI message (optionally with edited content)"""
        now = datetime.utcnow()
        if edited_content:
            query = """
                UPDATE messages
                SET ai_is_valid = true, ai_edited = true,
                    content = $2, updated_at = $3
                WHERE id = $1 AND sender_type = 'ai_role'
                RETURNING *
            """
            row = await self.fetchrow(query, message_id, edited_content, now)
        else:
            query = """
                UPDATE messages
                SET ai_is_valid = true, updated_at = $2
                WHERE id = $1 AND sender_type = 'ai_role'
                RETURNING *
            """
            row = await self.fetchrow(query, message_id, now)
        return self._row_to_message(row) if row else None

    async def reject(self, message_id: UUID) -> Optional[Message]:
        """Reject AI message (set ai_is_valid = false)"""
        now = datetime.utcnow()
        query = """
            UPDATE messages
            SET ai_is_valid = false, updated_at = $2
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

    async def messages_exist_from_sender(
        self, chat_id: UUID, sender_id: UUID,
    ) -> bool:
        """True iff at least one non-deleted message from `sender_id` exists in `chat_id`.
        Used by submit-poll to validate that the assignee actually replied to the AI
        before allowing summary generation. Cheap: SELECT 1 ... LIMIT 1.
        """
        query = (
            "SELECT 1 FROM messages "
            "WHERE chat_id = $1 AND sender_id = $2 AND is_deleted = false "
            "LIMIT 1"
        )
        row = await self.fetchrow(query, chat_id, sender_id)
        return row is not None

    async def find_reply(self, reply_to_id: UUID, sender_id: UUID) -> Optional[Message]:
        """Find an existing reply by sender to a specific message.

        Used by the reply-to-mention endpoint to enforce single-use semantics:
        a user may reply to a given mentioning message at most once via this path.

        Намеренно НЕ фильтруем `is_deleted` — single-use значит single-use.
        Если reply был отправлен и потом soft-deleted (например, модератором),
        второй reply всё равно запрещён. Если в будущем продукт решит «удалённое
        не считается» — снять оговорку и добавить `AND is_deleted = false`.
        """
        row = await self.fetchrow(
            "SELECT * FROM messages WHERE reply_to_id = $1 AND sender_id = $2 LIMIT 1",
            reply_to_id, sender_id,
        )
        return self._row_to_message(row) if row else None

    def _row_to_message(self, row) -> Message:
        """Convert database row to Message"""
        mentions_data = row["mentions"]
        if isinstance(mentions_data, str):
            mentions_data = json.loads(mentions_data)
        mentions = [Mention.from_dict(m) for m in (mentions_data or [])]

        keys = set(row.keys())

        # metadata column may be absent in legacy fixture rows (older test schemas);
        # gracefully default to {}. asyncpg returns jsonb as already-decoded dict,
        # but accept str too for robustness against custom fetchers.
        if "metadata" in keys:
            metadata_raw = row["metadata"]
            if isinstance(metadata_raw, str):
                metadata = json.loads(metadata_raw)
            else:
                metadata = metadata_raw or {}
        else:
            metadata = {}

        return Message(
            id=row["id"],
            chat_id=row["chat_id"],
            sender_type=SenderType(row["sender_type"]),
            sender_id=row["sender_id"],
            content=row["content"],
            mentions=mentions,
            reply_to_id=row["reply_to_id"],
            ai_is_valid=row["ai_is_valid"],
            ai_edited=row["ai_edited"],
            is_deleted=row["is_deleted"],
            mem_id=row["mem_id"] if "mem_id" in keys else None,
            metadata=metadata,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
