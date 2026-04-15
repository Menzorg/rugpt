"""
Chat Service

Business logic for chats and messages.
"""
import logging
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from ..models.chat import Chat, ChatType
from ..models.message import Message, Mention, SenderType, MentionType
from ..storage.chat_storage import ChatStorage
from ..storage.message_storage import MessageStorage

if TYPE_CHECKING:
    from ..models.task import Task
    from ..models.user import User
    from ..storage.task_storage import TaskStorage
    from ..storage.project_storage import ProjectStorage


def _can_see_task(task: "Task", user: "User") -> bool:
    """Strict task visibility: creator, current assignee, or same-org admin."""
    if task.org_id != user.org_id and not user.is_admin:
        return False
    if user.id == task.created_by_user_id:
        return True
    if user.id == task.assignee_user_id:
        return True
    if user.is_admin and task.org_id == user.org_id:
        return True
    return False


def filter_sidebar_task_chats(
    chats: List[Chat],
    tasks_by_id: dict,
    user: "User",
) -> List[Chat]:
    """Pure helper: keep only chats whose task passes can_see_task and is
    active non-done. Used by sidebar endpoint; extracted for testability."""
    out: List[Chat] = []
    for chat in chats:
        if chat.task_id is None or not chat.is_active:
            continue
        task = tasks_by_id.get(chat.task_id)
        if task is None or not task.is_active or task.status == "done":
            continue
        if not _can_see_task(task, user):
            continue
        out.append(chat)
    return out


def filter_sidebar_project_chats(
    chats: List[Chat],
    projects_by_id: dict,
    user: "User",
) -> List[Chat]:
    """Keep active project chats whose project is active and viewer is participant."""
    out: List[Chat] = []
    for chat in chats:
        if chat.project_id is None or not chat.is_active:
            continue
        project = projects_by_id.get(chat.project_id)
        if project is None or not project.is_active:
            continue
        if user.id not in chat.participants:
            continue
        out.append(chat)
    return out

logger = logging.getLogger("rugpt.services.chat")


class ChatService:
    """Service for chat operations"""

    def __init__(self, chat_storage: ChatStorage, message_storage: MessageStorage):
        self.chat_storage = chat_storage
        self.message_storage = message_storage

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

    async def get_chat(self, chat_id: UUID) -> Optional[Chat]:
        """Get chat by ID"""
        return await self.chat_storage.get_by_id(chat_id)

    async def list_user_chats(
        self,
        user_id: UUID,
        active_only: bool = True,
        chat_type: Optional[str] = None,
    ) -> List[Chat]:
        """List chats for user, optionally filtered by type ('direct'|'task'|'project')."""
        return await self.chat_storage.list_by_user(user_id, active_only, chat_type)

    # ============================================
    # Task chats
    # ============================================

    async def create_task_chat(
        self,
        task_id: UUID,
        org_id: UUID,
        creator_id: UUID,
        assignee_id: UUID,
    ) -> Chat:
        """Create a chat attached to a task. Participants = {creator, assignee}."""
        participants = list({creator_id, assignee_id})
        chat = Chat(
            org_id=org_id,
            type=ChatType.TASK,
            task_id=task_id,
            participants=participants,
            created_by=creator_id,
        )
        return await self.chat_storage.create(chat)

    async def get_task_chat(self, task_id: UUID) -> Optional[Chat]:
        """Get chat attached to a task (if any)."""
        return await self.chat_storage.get_by_task_id(task_id)

    async def add_task_chat_participant(
        self, task_id: UUID, user_id: UUID,
    ) -> bool:
        """Add a participant to the task's chat (thin wrapper)."""
        chat = await self.chat_storage.get_by_task_id(task_id)
        if not chat:
            return False
        return await self.add_participant(chat.id, user_id)

    async def archive_task_chat(self, task_id: UUID) -> None:
        """Soft-archive a task chat. Participants & messages are preserved."""
        chat = await self.chat_storage.get_by_task_id(task_id)
        if chat and chat.is_active:
            chat.is_active = False
            await self.chat_storage.update(chat)

    # ============================================
    # Project chats
    # ============================================

    async def ensure_project_chat_membership(
        self,
        project_id: UUID,
        org_id: UUID,
        user_ids: List[UUID],
    ) -> Chat:
        """Create-or-merge a project chat with the given participants.

        Idempotent: if the chat exists and was archived, it is reactivated
        (closes the archive/create race described in spec resolved #7).
        """
        existing = await self.chat_storage.get_by_project_id(project_id)
        dedup_new = [u for u in user_ids if u is not None]
        if existing:
            changed = False
            if not existing.is_active:
                existing.is_active = True
                changed = True
            merged = list(dict.fromkeys([*existing.participants, *dedup_new]))
            if merged != existing.participants:
                existing.participants = merged
                changed = True
            if changed:
                await self.chat_storage.update(existing)
            return existing
        chat = Chat(
            org_id=org_id,
            type=ChatType.PROJECT,
            project_id=project_id,
            participants=list(dict.fromkeys(dedup_new)),
        )
        return await self.chat_storage.create(chat)

    async def archive_project_chat(self, project_id: UUID) -> None:
        """Soft-archive a project chat."""
        chat = await self.chat_storage.get_by_project_id(project_id)
        if chat and chat.is_active:
            chat.is_active = False
            await self.chat_storage.update(chat)

    # ============================================
    # Sidebar helpers
    # ============================================

    async def list_user_task_chats_for_sidebar(
        self,
        user: "User",
        task_storage: "TaskStorage",
    ) -> List[Chat]:
        """Task chats visible to user in the sidebar: active task, non-done
        status, viewer passes can_see_task. Task storage is passed in to
        avoid a hard dependency on TaskService inside ChatService."""
        chats = await self.chat_storage.list_by_user(
            user.id, active_only=True, chat_type="task",
        )
        task_ids = [c.task_id for c in chats if c.task_id is not None]
        tasks_by_id = await task_storage.get_many_by_ids(task_ids) if task_ids else {}
        return filter_sidebar_task_chats(chats, tasks_by_id, user)

    async def list_user_project_chats_for_sidebar(
        self,
        user: "User",
        project_storage: "ProjectStorage",
    ) -> List[Chat]:
        """Project chats visible in the sidebar: active project + viewer is participant."""
        chats = await self.chat_storage.list_by_user(
            user.id, active_only=True, chat_type="project",
        )
        project_ids = [c.project_id for c in chats if c.project_id is not None]
        projects_by_id = await project_storage.get_many_by_ids(project_ids) if project_ids else {}
        return filter_sidebar_project_chats(chats, projects_by_id, user)

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
            ai_is_valid=True if sender_type == SenderType.USER else None,  # User=auto-valid, AI=pending
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
        """Approve AI message (optionally with edits)"""
        message = await self.message_storage.validate(message_id, edited_content)
        if message:
            logger.info(f"AI message {message_id} approved (edited={edited_content is not None})")
        return message

    async def reject_ai_message(self, message_id: UUID) -> Optional[Message]:
        """Reject AI message (set ai_is_valid = false)"""
        message = await self.message_storage.reject(message_id)
        if message:
            logger.info(f"AI message {message_id} rejected")
        return message

    async def get_pending_review_messages(self, user_id: UUID) -> List[Message]:
        """Get AI messages pending review by user"""
        return await self.message_storage.list_pending_review(user_id)

    async def delete_message(self, message_id: UUID) -> bool:
        """Delete message"""
        return await self.message_storage.delete(message_id)
