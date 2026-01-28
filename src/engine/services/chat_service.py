"""
Chat Service

Business logic for chats and messages.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID, uuid4

from ..models.chat import Chat, ChatType
from ..models.message import Message, Mention, SenderType, MentionType
from ..storage.chat_storage import ChatStorage
from ..storage.message_storage import MessageStorage

logger = logging.getLogger("rugpt.services.chat")


class ChatService:
    """Service for chat operations"""

    def __init__(self, chat_storage: ChatStorage, message_storage: MessageStorage):
        self.chat_storage = chat_storage
        self.message_storage = message_storage

    async def create_main_chat(self, user_id: UUID, org_id: UUID) -> Chat:
        """Create main chat for user"""
        chat = Chat(
            id=uuid4(),
            org_id=org_id,
            type=ChatType.MAIN,
            name=None,
            participants=[user_id],
            created_by=user_id,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        return await self.chat_storage.create(chat)

    async def get_or_create_main_chat(self, user_id: UUID, org_id: UUID) -> Chat:
        """Get or create user's main chat"""
        chat = await self.chat_storage.get_main_chat(user_id)
        if not chat:
            chat = await self.create_main_chat(user_id, org_id)
            logger.info(f"Created main chat {chat.id} for user {user_id}")
        return chat

    async def create_direct_chat(
        self,
        user1_id: UUID,
        user2_id: UUID,
        org_id: UUID
    ) -> Chat:
        """Create or get direct chat between two users"""
        existing = await self.chat_storage.get_direct_chat(user1_id, user2_id)
        if existing:
            return existing

        chat = Chat(
            id=uuid4(),
            org_id=org_id,
            type=ChatType.DIRECT,
            name=None,
            participants=[user1_id, user2_id],
            created_by=user1_id,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        return await self.chat_storage.create(chat)

    async def create_group_chat(
        self,
        name: str,
        participants: List[UUID],
        created_by: UUID,
        org_id: UUID
    ) -> Chat:
        """Create group chat"""
        chat = Chat(
            id=uuid4(),
            org_id=org_id,
            type=ChatType.GROUP,
            name=name,
            participants=participants,
            created_by=created_by,
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        return await self.chat_storage.create(chat)

    async def get_chat(self, chat_id: UUID) -> Optional[Chat]:
        """Get chat by ID"""
        return await self.chat_storage.get_by_id(chat_id)

    async def list_user_chats(self, user_id: UUID, active_only: bool = True) -> List[Chat]:
        """List chats for user"""
        return await self.chat_storage.list_by_user(user_id, active_only)

    async def add_participant(self, chat_id: UUID, user_id: UUID) -> bool:
        """Add participant to chat"""
        return await self.chat_storage.add_participant(chat_id, user_id)

    async def remove_participant(self, chat_id: UUID, user_id: UUID) -> bool:
        """Remove participant from chat"""
        return await self.chat_storage.remove_participant(chat_id, user_id)

    async def archive_chat(self, chat_id: UUID) -> bool:
        """Archive (soft delete) chat"""
        return await self.chat_storage.delete(chat_id)

    # Message operations

    async def send_message(
        self,
        chat_id: UUID,
        sender_id: UUID,
        content: str,
        sender_type: SenderType = SenderType.USER,
        mentions: Optional[List[Mention]] = None,
        reply_to_id: Optional[UUID] = None,
    ) -> Message:
        """Send a message to chat"""
        message = Message(
            id=uuid4(),
            chat_id=chat_id,
            sender_type=sender_type,
            sender_id=sender_id,
            content=content,
            mentions=mentions or [],
            reply_to_id=reply_to_id,
            ai_validated=sender_type == SenderType.USER,  # User messages auto-validated
            ai_edited=False,
            is_deleted=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        created = await self.message_storage.create(message)
        await self.chat_storage.update_last_message(chat_id)
        logger.info(f"Message {created.id} sent to chat {chat_id}")
        return created

    async def get_message(self, message_id: UUID) -> Optional[Message]:
        """Get message by ID"""
        return await self.message_storage.get_by_id(message_id)

    async def list_messages(
        self,
        chat_id: UUID,
        limit: int = 50,
        before_id: Optional[UUID] = None
    ) -> List[Message]:
        """List messages in chat"""
        return await self.message_storage.list_by_chat(chat_id, limit, before_id)

    async def validate_ai_message(
        self,
        message_id: UUID,
        edited_content: Optional[str] = None
    ) -> Optional[Message]:
        """Validate AI message (optionally with edits)"""
        message = await self.message_storage.validate(message_id, edited_content)
        if message:
            logger.info(f"AI message {message_id} validated (edited={edited_content is not None})")
        return message

    async def get_unvalidated_messages(self, user_id: UUID) -> List[Message]:
        """Get AI messages pending validation by user"""
        return await self.message_storage.list_unvalidated(user_id)

    async def delete_message(self, message_id: UUID) -> bool:
        """Delete message"""
        return await self.message_storage.delete(message_id)
