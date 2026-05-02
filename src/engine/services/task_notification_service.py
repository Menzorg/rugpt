"""
TaskNotificationService (item 10).

Posts plain-text notifications from the PM agent into direct chats between PM and
interested users when task state changes. Lazily creates the direct chat on first
notification.

Addressing rule: notify everyone involved in the task except the actor.
- Recipients = active(union(creator, assignee, participants)) minus {actor}
- Scheduler (overdue) -> notify everyone (no actor)
- Texts are written in third person/neutral (no "Ваш"/"Ваша") since multiple
  parties may receive the same message.

Delivery: message is persisted via message_storage.create, then published to
Kafka topic `chat.events` so the NestJS SocketGateway can broadcast it to
connected clients in real time. If Kafka is disabled (Config.KAFKA_ENABLED=false),
only DB persist happens — clients see the message on next page load.

Note on text register: broadcast transitions (the 9 notify_* methods that go to
multiple recipients) use neutral third-person text since the same string reaches
creator, assignee, and participants. The two single-recipient methods
(notify_added_as_participant / notify_removed_as_participant) use second person
("Вас добавили...") since there's exactly one addressee.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from ..config import Config
from ..models.message import Message, SenderType
from ..models.task import Task

if TYPE_CHECKING:
    from ..models.user import User
    from ..storage.user_storage import UserStorage
    from ..storage.message_storage import MessageStorage
    from ..storage.task_participant_storage import TaskParticipantStorage
    from .chat_service import ChatService
    from ..kafka.producer import KafkaProducerService

logger = logging.getLogger("rugpt.services.task_notification")


class TaskNotificationService:
    def __init__(
        self,
        chat_service: "ChatService",
        message_storage: "MessageStorage",
        user_storage: "UserStorage",
        kafka_producer: Optional["KafkaProducerService"] = None,
        task_participant_storage: Optional["TaskParticipantStorage"] = None,
    ):
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.user_storage = user_storage
        self.kafka_producer = kafka_producer
        self.task_participant_storage = task_participant_storage
        self._pm_user_id: Optional[UUID] = None

    async def _get_pm_user_id(self) -> Optional[UUID]:
        if self._pm_user_id is not None:
            return self._pm_user_id
        pm = await self.user_storage.get_system_user_by_username("pm")
        if pm is None:
            logger.error(
                "PM system user not found — migration 018 not applied or seed failed"
            )
            return None
        self._pm_user_id = pm.id
        return self._pm_user_id

    async def _post(
        self,
        recipient_user_id: UUID,
        recipient_org_id: UUID,
        text: str,
        task_id: UUID,
    ) -> None:
        """Lazy-create direct chat PM<->recipient and post a message from PM."""
        logger.info(
            f"PM notify: recipient={recipient_user_id} task={task_id}"
        )
        pm_id = await self._get_pm_user_id()
        if pm_id is None:
            logger.warning(f"PM notify dropped: PM system user not found (task={task_id})")
            return

        chat = await self.chat_service.create_direct_chat(
            pm_id, recipient_user_id, recipient_org_id
        )

        msg = Message(
            id=uuid4(),
            chat_id=chat.id,
            sender_type=SenderType.AI_ROLE,
            sender_id=pm_id,
            content=text,
            ai_is_valid=True,  # auto-validated; this is a system notification, not a response to review
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        created = await self.message_storage.create(msg)
        # Touch chat last_message_at so sidebar sorting reflects the notification.
        await self.chat_service.chat_storage.update_last_message(chat.id)

        # Publish to chat.events for real-time WS delivery via NestJS consumer.
        if self.kafka_producer is not None:
            try:
                await self.kafka_producer.send(
                    Config.KAFKA_TOPIC_CHAT_EVENTS,
                    {
                        "chat_id": str(chat.id),
                        "message": created.to_dict(),
                    },
                    key=str(chat.id),
                )
            except Exception as e:
                logger.error(f"Failed to publish PM notification to Kafka: {e}")

        logger.info(
            f"PM -> user={recipient_user_id} task={task_id}: {text[:80]}"
        )

    # ============================================
    # Recipient resolution
    # ============================================

    def _actor_name(self, user: "User") -> str:
        return f"@{user.username}" if user.username else user.name

    async def _resolve_recipients(
        self, task: Task, exclude_user_id: Optional[UUID] = None,
    ) -> List["User"]:
        """
        Returns active users involved in the task: creator + assignee + participants.
        Deduplicates and excludes the actor (or any other user_id passed in).
        Filters out users with is_active=false.
        """
        candidate_ids: List[UUID] = []
        if task.created_by_user_id:
            candidate_ids.append(task.created_by_user_id)
        if task.assignee_user_id:
            candidate_ids.append(task.assignee_user_id)
        if self.task_participant_storage is not None:
            participant_ids = await self.task_participant_storage.list_user_ids(task.id)
            candidate_ids.extend(participant_ids)

        # Dedup + exclude
        seen = set()
        unique_ids: List[UUID] = []
        for uid in candidate_ids:
            if uid is None or uid == exclude_user_id or uid in seen:
                continue
            seen.add(uid)
            unique_ids.append(uid)

        # Load + filter inactive
        result: List["User"] = []
        for uid in unique_ids:
            user = await self.user_storage.get_by_id(uid)
            if user is None:
                continue
            # task_participant_storage.list_user_ids returns inactive users too (by design,
            # for chat-sync). We MUST filter them here so notifications don't leak to disabled accounts.
            # Fail closed: if a future User model lacks is_active, default to "skip".
            if not getattr(user, "is_active", False):
                continue
            result.append(user)
        return result

    async def _post_to_recipients(
        self, task: Task, actor_user_id: Optional[UUID], text: str,
    ) -> None:
        recipients = await self._resolve_recipients(task, exclude_user_id=actor_user_id)
        for u in recipients:
            await self._post(u.id, task.org_id, text, task.id)

    # ============================================
    # Notifications by transition
    # ============================================

    async def notify_take(self, task: Task, actor: "User") -> None:
        """Assignee took the task. Notify everyone else involved."""
        text = (
            f"{self._actor_name(actor)} взял задачу «{task.title}» в работу.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_mark_done(self, task: Task, actor: "User") -> None:
        """Assignee marked task done (awaiting review). Notify everyone else."""
        text = (
            f"{self._actor_name(actor)} отметил задачу «{task.title}» готовой. "
            f"Ожидает приёмки.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_accept(self, task: Task, actor: "User") -> None:
        """Creator accepted task. Notify everyone else."""
        text = f"Задача «{task.title}» принята."
        await self._post_to_recipients(task, actor.id, text)

    async def notify_reject(
        self, task: Task, actor: "User", comment: Optional[str] = None,
    ) -> None:
        """Creator rejected task back to in_progress. Notify everyone else."""
        text = f"Задача «{task.title}» возвращена в работу."
        if comment:
            text += f"\nПричина: {comment}"
        text += f"\n→ Открыть: /chat/task/{task.id}"
        await self._post_to_recipients(task, actor.id, text)

    async def notify_set_deadline(self, task: Task, actor: "User") -> None:
        """Deadline changed directly. Notify everyone else."""
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = (
            f"Срок задачи «{task.title}» изменён: {deadline_str}\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_propose_deadline(self, task: Task, actor: "User") -> None:
        """Assignee proposed a new deadline. Notify everyone else."""
        proposed_str = (
            task.proposed_deadline.strftime("%d.%m.%Y %H:%M")
            if task.proposed_deadline else "—"
        )
        text = (
            f"{self._actor_name(actor)} предложил перенести срок задачи «{task.title}» "
            f"на {proposed_str}.\n"
            f"→ Принять или отклонить: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor.id, text)

    async def notify_accept_proposed_deadline(self, task: Task, actor: "User") -> None:
        """Creator accepted assignee's proposed deadline. Notify everyone else."""
        deadline_str = task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        text = f"Предложение нового срока для «{task.title}» принято: {deadline_str}"
        await self._post_to_recipients(task, actor.id, text)

    async def notify_reject_proposed_deadline(self, task: Task, actor: "User") -> None:
        """Creator rejected assignee's proposed deadline. Notify everyone else."""
        text = f"Предложение нового срока для «{task.title}» отклонено."
        await self._post_to_recipients(task, actor.id, text)

    async def notify_overdue(self, task: Task) -> None:
        """Scheduler-driven; no actor — notify everyone."""
        text = (
            f"Задача «{task.title}» просрочена.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post_to_recipients(task, actor_user_id=None, text=text)

    # ============================================
    # Participant membership notifications
    # ============================================

    async def notify_added_as_participant(
        self, task: Task, user_id: UUID, by_user_id: Optional[UUID] = None,
    ) -> None:
        """Notify a single user that they were added as a participant of a task."""
        if by_user_id is not None and user_id == by_user_id:
            return  # don't notify yourself for self-add
        user = await self.user_storage.get_by_id(user_id)
        if user is None or not getattr(user, "is_active", False):
            return
        text = (
            f"Вас добавили в задачу «{task.title}» как участника.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post(user_id, task.org_id, text, task.id)

    async def notify_removed_as_participant(
        self, task: Task, user_id: UUID, by_user_id: Optional[UUID] = None,
    ) -> None:
        """Notify a single user that they were removed from task participants."""
        if by_user_id is not None and user_id == by_user_id:
            return
        user = await self.user_storage.get_by_id(user_id)
        if user is None or not getattr(user, "is_active", False):
            return
        text = f"Вас исключили из задачи «{task.title}»."
        await self._post(user_id, task.org_id, text, task.id)
