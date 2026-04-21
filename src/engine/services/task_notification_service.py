"""
TaskNotificationService (item 10).

Posts plain-text notifications from the PM agent into direct chats between PM and
interested users when task state changes. Lazily creates the direct chat on first
notification.

Adressing rule: notify the party who did NOT initiate the change.
- Assignee transitions -> notify creator
- Creator transitions -> notify assignee
- Scheduler (overdue) -> notify both

Delivery: message is persisted via message_storage.create, then published to
Kafka topic `chat.events` so the NestJS SocketGateway can broadcast it to
connected clients in real time. If Kafka is disabled (Config.KAFKA_ENABLED=false),
only DB persist happens — clients see the message on next page load.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from ..config import Config
from ..models.message import Message, SenderType
from ..models.task import Task

if TYPE_CHECKING:
    from ..models.user import User
    from ..storage.user_storage import UserStorage
    from ..storage.message_storage import MessageStorage
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
    ):
        self.chat_service = chat_service
        self.message_storage = message_storage
        self.user_storage = user_storage
        self.kafka_producer = kafka_producer
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
    # Notifications by transition
    # ============================================

    async def _load_user(self, user_id: Optional[UUID]):
        if user_id is None:
            return None
        return await self.user_storage.get_by_id(user_id)

    def _actor_name(self, user: "User") -> str:
        return f"@{user.username}" if user.username else user.name

    async def notify_take(self, task: Task, actor: "User") -> None:
        """Assignee took the task. Notify creator."""
        creator = await self._load_user(task.created_by_user_id)
        if creator is None or creator.id == actor.id:
            return
        text = (
            f"{self._actor_name(actor)} взял задачу «{task.title}» в работу.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_mark_done(self, task: Task, actor: "User") -> None:
        """Assignee marked task done (awaiting review). Notify creator."""
        creator = await self._load_user(task.created_by_user_id)
        if creator is None or creator.id == actor.id:
            return
        text = (
            f"{self._actor_name(actor)} отметил задачу «{task.title}» готовой. "
            f"Ожидает вашей приёмки.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_accept(self, task: Task, actor: "User") -> None:
        """Creator accepted task. Notify assignee."""
        assignee = await self._load_user(task.assignee_user_id)
        if assignee is None or assignee.id == actor.id:
            return
        text = f"Ваша работа «{task.title}» принята. Спасибо."
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_reject(
        self, task: Task, actor: "User", comment: Optional[str] = None
    ) -> None:
        """Creator rejected task back to in_progress. Notify assignee."""
        assignee = await self._load_user(task.assignee_user_id)
        if assignee is None or assignee.id == actor.id:
            return
        text = f"Задача «{task.title}» возвращена в работу."
        if comment:
            text += f"\nПричина: {comment}"
        text += f"\n→ Открыть: /chat/task/{task.id}"
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_set_deadline(self, task: Task, actor: "User") -> None:
        """Creator changed deadline directly. Notify assignee."""
        assignee = await self._load_user(task.assignee_user_id)
        if assignee is None or assignee.id == actor.id:
            return
        deadline_str = (
            task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        )
        text = (
            f"Срок задачи «{task.title}» изменён: {deadline_str}\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_propose_deadline(self, task: Task, actor: "User") -> None:
        """Assignee proposed a new deadline. Notify creator."""
        creator = await self._load_user(task.created_by_user_id)
        if creator is None or creator.id == actor.id:
            return
        proposed_str = (
            task.proposed_deadline.strftime("%d.%m.%Y %H:%M")
            if task.proposed_deadline
            else "—"
        )
        text = (
            f"{self._actor_name(actor)} предложил перенести срок задачи «{task.title}» "
            f"на {proposed_str}.\n"
            f"→ Принять или отклонить: /chat/task/{task.id}"
        )
        await self._post(creator.id, task.org_id, text, task.id)

    async def notify_accept_proposed_deadline(self, task: Task, actor: "User") -> None:
        """Creator accepted assignee's proposed deadline. Notify assignee."""
        assignee = await self._load_user(task.assignee_user_id)
        if assignee is None or assignee.id == actor.id:
            return
        deadline_str = (
            task.deadline.strftime("%d.%m.%Y %H:%M") if task.deadline else "—"
        )
        text = f"Ваше предложение нового срока для «{task.title}» принято: {deadline_str}"
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_reject_proposed_deadline(self, task: Task, actor: "User") -> None:
        """Creator rejected assignee's proposed deadline. Notify assignee."""
        assignee = await self._load_user(task.assignee_user_id)
        if assignee is None or assignee.id == actor.id:
            return
        text = f"Ваше предложение нового срока для «{task.title}» отклонено."
        await self._post(assignee.id, task.org_id, text, task.id)

    async def notify_overdue(self, task: Task) -> None:
        """Task became overdue (scheduler-driven). Notify both sides."""
        assignee = await self._load_user(task.assignee_user_id)
        creator = await self._load_user(task.created_by_user_id)
        text = (
            f"Задача «{task.title}» просрочена.\n"
            f"→ Открыть: /chat/task/{task.id}"
        )
        if assignee is not None:
            await self._post(assignee.id, task.org_id, text, task.id)
        if creator is not None and (assignee is None or creator.id != assignee.id):
            await self._post(creator.id, task.org_id, text, task.id)
