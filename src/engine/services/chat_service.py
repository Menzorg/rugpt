"""
Chat Service

Business logic for chats and messages.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID, uuid4

from ..config import Config
from ..models.chat import Chat, ChatType
from ..models.message import Message, Mention, SenderType, MentionType
from ..storage.chat_storage import ChatStorage
from ..storage.chat_read_state_storage import ChatReadStateStorage
from ..storage.message_storage import MessageStorage

if TYPE_CHECKING:
    from ..models.task import Task
    from ..models.user import User
    from ..storage.task_storage import TaskStorage
    from ..storage.project_storage import ProjectStorage
    from ..storage.user_file_storage import UserFileStorage
    from ..storage.message_attachment_storage import MessageAttachmentStorage

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

logger = get_logger("services")

class ChatService:
    """Service for chat operations"""

    def __init__(
        self,
        chat_storage: ChatStorage,
        message_storage: MessageStorage,
        chat_read_state_storage: Optional[ChatReadStateStorage] = None,
        user_file_storage: Optional["UserFileStorage"] = None,
        message_attachment_storage: Optional["MessageAttachmentStorage"] = None,
    ):
        self.chat_storage = chat_storage
        self.message_storage = message_storage
        self.chat_read_state_storage = chat_read_state_storage
        self.user_file_storage = user_file_storage
        self.message_attachment_storage = message_attachment_storage

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
        created = await self.chat_storage.create(chat)
        logger.info(
            f"create_direct_chat: id={created.id} users=[{user1_id}, {user2_id}] org={org_id}"
        )
        return created

    async def get_chat(self, chat_id: UUID) -> Optional[Chat]:
        """Get chat by ID"""
        return await self.chat_storage.get_by_id(chat_id)

    async def can_user_access_chat(self, user: "User", chat: Chat) -> bool:
        """Single-point access check for any chat in the system.

        Strict orgship by default. The ONLY cross-org exemption is for
        ChatType.SUPPORT — never broadened to other chat types.

        SUPPORT exemption rules:
        - A user listed in chat.participants always has access (e.g. requester
          from the customer org, or an operator who already took the ticket).
        - An operator from the RuGPT Support org may preview a SUPPORT chat
          before joining as participant (queue/take flow), but ONLY if the
          chat carries a non-null support_ticket_id. The double condition
          guards against pathological/legacy SUPPORT rows lacking a ticket.

        For all non-SUPPORT chats: orgship is the gate. A user must belong
        to the same org as the chat AND appear in chat.participants. The
        SUPPORT exemption MUST NOT bleed into DIRECT/TASK/PROJECT.

        Args:
            user: User attempting access.
            chat: Chat to access.

        Returns:
            True if access permitted, False otherwise.
        """
        # SUPPORT exemption — bounded strictly by chat.type
        if chat.type == ChatType.SUPPORT:
            if user.id in chat.participants:
                return True
            # RuGPT Support operator can preview tickets in queue (before take)
            if (
                user.org_id == Config.RUGPT_SUPPORT_ORG_ID
                and chat.support_ticket_id is not None
            ):
                return True
            return False

        # Non-SUPPORT chats: strict orgship + participant
        # Default branch: any future ChatType (other than SUPPORT) falls through
        # to strict orgship + participant. Fail-closed by design — do not add
        # per-type carve-outs here without re-running the security regression suite.
        if chat.org_id != user.org_id:
            return False
        return user.id in chat.participants

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
        created = await self.chat_storage.create(chat)
        logger.info(
            f"create_task_chat: id={created.id} task={task_id} participants={participants}"
        )
        return created

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
    # Poll chats
    # ============================================

    async def create_poll_chat(
        self,
        poll_id: UUID,
        assignee_user_id: UUID,
        interviewer_user_id: UUID,
        org_id: UUID,
    ) -> Chat:
        """Idempotent creation of a poll-scoped chat.

        Participants: [assignee, poll_interviewer_ai].
        Returns existing chat if one already exists for this poll_id.
        """
        existing = await self.chat_storage.get_by_poll_id(poll_id)
        if existing is not None:
            return existing

        chat = Chat(
            org_id=org_id,
            type=ChatType.POLL,
            participants=[assignee_user_id, interviewer_user_id],
            poll_id=poll_id,
        )
        created = await self.chat_storage.create(chat)
        logger.info(
            f"create_poll_chat: id={created.id} poll={poll_id} "
            f"assignee={assignee_user_id} interviewer={interviewer_user_id}"
        )
        return created

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
        file_ids: Optional[List[UUID]] = None,
    ) -> Message:
        """Send a message to chat.

        Args:
            file_ids: optional list of UserFile ids to attach (max 5).
                Each file must be owned by sender_id and active. Validation runs
                BEFORE message persist so a rejected request never orphans a row.
        """
        # Validate attachments up-front (fail fast, no orphan message).
        if file_ids:
            if len(file_ids) > 5:
                raise ValueError("Maximum 5 attachments per message")
            if self.user_file_storage is None or self.message_attachment_storage is None:
                raise RuntimeError(
                    "ChatService missing user_file_storage/message_attachment_storage "
                    "dependencies — cannot save messages with attachments"
                )
            for fid in file_ids:
                file = await self.user_file_storage.get_by_id(fid)
                if file is None or not file.is_active:
                    raise ValueError(f"File {fid} not found or inactive")
                if file.user_id != sender_id:
                    raise PermissionError(f"File {fid} does not belong to sender")

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

        # Link attachments after persist (need created.id) and hydrate the
        # returned message so the caller doesn't need a second round-trip.
        # Non-transactional: if attach fails after the message is persisted, the
        # message lives without attachments and the caller sees attachments=[].
        # Acceptable for MVP — attach is idempotent (ON CONFLICT DO NOTHING in
        # message_attachment_storage), so a manual retry path could re-link.
        # If consistency becomes critical, wrap (create, attach) in a single
        # transaction at the storage layer.
        if file_ids:
            await self.message_attachment_storage.attach(created.id, file_ids)
            created.attachments = await self.message_attachment_storage.get_for_message(
                created.id,
            )

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

    async def get_reviewed_messages(self, user_id: UUID, limit: int = 50) -> List[Message]:
        """List AI messages already validated/rejected by user."""
        return await self.message_storage.list_reviewed(user_id, limit)

    async def get_pending_review_messages(self, user_id: UUID) -> List[Message]:
        """Get AI messages pending review by user"""
        return await self.message_storage.list_pending_review(user_id)

    async def delete_message(self, message_id: UUID) -> bool:
        """Delete message"""
        return await self.message_storage.delete(message_id)

    # ============================================
    # Read-state (unread tracking)
    # ============================================

    async def mark_chat_read(
        self,
        chat_id: UUID,
        user_id: UUID,
        message_id: UUID,
    ) -> None:
        """Mark messages in `chat_id` up to and including `message_id` as read by `user_id`.

        Validation:
            - Message must exist and belong to `chat_id` → ValueError if not.
            - User must be a participant of the chat → PermissionError if not.

        Delegates persistence to ChatReadStateStorage.upsert with the message's
        `created_at` as the high-water-mark. The storage layer enforces the
        monotonic guard (HWM never moves backward).
        """
        msg = await self.message_storage.get_by_id(message_id)
        if msg is None or msg.chat_id != chat_id:
            raise ValueError("Message not in chat")

        chat = await self.chat_storage.get_by_id(chat_id)
        if chat is None or not chat.is_active or user_id not in chat.participants:
            raise PermissionError("Not a participant")

        await self.chat_read_state_storage.upsert(
            chat_id, user_id, message_id, msg.created_at,
        )
        return None

    async def list_unread_counts(
        self,
        user_id: UUID,
        org_id: UUID,
    ) -> dict:
        """Return {chat_id: unread_count} for all chats of `user_id` in `org_id`.

        Thin pass-through to ChatReadStateStorage.get_unread_counts_for_user.
        Only chats with count > 0 appear; counts cap at 100.
        """
        return await self.chat_read_state_storage.get_unread_counts_for_user(
            user_id, org_id,
        )

    async def user_can_access_attached_file(
        self, user_id: UUID, file_id: UUID, org_id: UUID,
    ) -> bool:
        """True if user_id is a participant of any chat where file_id is attached.

        Used to gate POST /files/{id}/clone — caller must have seen the file in
        a chat they participate in.
        """
        if self.message_attachment_storage is None:
            return False
        return await self.message_attachment_storage.is_file_visible_to_user(
            file_id=file_id, user_id=user_id, org_id=org_id,
        )
