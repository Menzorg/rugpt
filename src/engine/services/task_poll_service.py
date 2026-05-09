"""
Task Poll Service

Business logic for daily morning polls.
Creates polls, processes responses, expires stale polls.
"""

from src.engine.unified_logger import get_logger
from datetime import datetime, date, timedelta
from typing import Optional, List
from uuid import UUID

import asyncpg

from ..models.task_poll import TaskPoll
from ..storage.task_poll_storage import TaskPollStorage
from .task_service import TaskService
from .in_app_notification_service import InAppNotificationService

logger = get_logger("services")

# poll_interviewer_ai system user lives in the RuGPT system org (migration 029).
# Duplicated here (rather than imported from ai_service) to avoid pulling the
# heavy AIService import chain into TaskPollService at module load.
_RUGPT_SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")
_POLL_INTERVIEWER_USERNAME = "poll_interviewer_ai"

class TaskPollService:

    def __init__(
        self,
        storage: TaskPollStorage,
        task_service: TaskService,
        in_app_notification_service: InAppNotificationService,
        poll_expire_hours: int = 10,
    ):
        self.storage = storage
        self.task_service = task_service
        self.notification_service = in_app_notification_service
        self.poll_expire_hours = poll_expire_hours
        # Wired post-construction by EngineService (circular-dep avoidance,
        # mirrors agent_executor.memory_service / agent_executor.correction_rule_service).
        # Optional: when None, create_daily_poll degrades gracefully to
        # poll-row + bell-notification only (no AI dialog chat).
        self.chat_service = None
        self.ai_service = None
        self.user_storage = None

    async def create_daily_poll(
        self,
        assignee_user_id: UUID,
        org_id: UUID,
    ) -> Optional[TaskPoll]:
        """
        Create a morning poll for an employee if they have active tasks.
        Returns None if no active tasks or poll already exists for today.
        Called by scheduler (morning_poll_job).

        Side effects when chat_service/ai_service/user_storage are wired:
          1. Creates a POLL chat (assignee + poll_interviewer_ai) via ChatService.
          2. Enqueues a poll_initial agent run on Kafka so the AI sends the first message.
          3. Sends an in-app bell notification.
        Kafka enqueue failures are logged and swallowed — auto-retry (Task 12)
        will pick stuck polls on the next scheduler tick.
        """
        today = date.today()

        # Check if poll already exists for today
        existing = await self.storage.get_by_user_and_date(assignee_user_id, today)
        if existing:
            logger.debug(f"Poll already exists for user {assignee_user_id} on {today}")
            return None

        # Check if user has active tasks
        active_tasks = await self.task_service.storage.list_active_for_polls(assignee_user_id)
        if not active_tasks:
            logger.debug(f"No active tasks for user {assignee_user_id}, skipping poll")
            return None

        expires_at = datetime.utcnow() + timedelta(hours=self.poll_expire_hours)

        poll = TaskPoll(
            org_id=org_id,
            assignee_user_id=assignee_user_id,
            poll_date=today,
            expires_at=expires_at,
            task_ids=[t.id for t in active_tasks],
        )
        try:
            created = await self.storage.create(poll)
        except asyncpg.UniqueViolationError:
            # Concurrent caller already created today's poll for this user (DB-level
            # UNIQUE INDEX idx_task_polls_unique_daily on (assignee_user_id, poll_date)
            # backstops the pre-check `get_by_user_and_date` race window). Treat as
            # idempotent and bail — the other caller is responsible for chat / enqueue.
            logger.info(
                f"create_daily_poll: race-lost UNIQUE on user={assignee_user_id} "
                f"date={today}; another caller is handling it"
            )
            return None
        logger.info(f"Created daily poll for user {assignee_user_id} ({len(active_tasks)} tasks)")

        # Create poll-chat and enqueue first AI message (if dependencies wired).
        # Without wiring, the row + bell still get created — frontend will show
        # the poll, just without an AI dialog.
        if (
            self.chat_service is not None
            and self.ai_service is not None
            and self.user_storage is not None
        ):
            interviewer = await self.user_storage.get_by_username(
                _POLL_INTERVIEWER_USERNAME, _RUGPT_SYSTEM_ORG_ID,
            )
            if interviewer is None:
                logger.error(
                    "poll_interviewer_ai user not found in DB; "
                    "did migration 029 run? Skipping chat/enqueue for poll %s.",
                    created.id,
                )
            else:
                chat = await self.chat_service.create_poll_chat(
                    poll_id=created.id,
                    assignee_user_id=assignee_user_id,
                    interviewer_user_id=interviewer.id,
                    org_id=org_id,
                )
                try:
                    await self.ai_service.enqueue_poll_initial(
                        created.id, chat.id, interviewer.id,
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to enqueue poll_initial for poll {created.id}: {e}",
                        exc_info=True,
                    )
                    # Don't fail the whole poll creation — auto-retry в скедулере
                    # подхватит на следующем tick'е (Task 12).

        # Notify employee via bell
        await self.notification_service.create(
            user_id=assignee_user_id,
            org_id=org_id,
            type="poll",
            title="Утренний опрос по задачам",
            content=f"У вас {len(active_tasks)} активных задач. Обновите статусы.",
            reference_type="task_poll",
            reference_id=created.id,
        )

        return created

    async def get(self, poll_id: UUID) -> Optional[TaskPoll]:
        """Get poll by ID"""
        return await self.storage.get_by_id(poll_id)

    async def get_today_poll(self, assignee_user_id: UUID) -> Optional[TaskPoll]:
        """Get today's poll for an employee"""
        return await self.storage.get_by_user_and_date(assignee_user_id, date.today())

    async def list_by_user(self, assignee_user_id: UUID, limit: int = 30) -> List[TaskPoll]:
        """List polls for a user"""
        return await self.storage.list_by_user(assignee_user_id, limit)

    async def list_by_org_and_date(self, org_id: UUID, poll_date: date) -> List[TaskPoll]:
        """List all polls for an org on a date (for evening report)"""
        return await self.storage.list_by_org_and_date(org_id, poll_date)

    async def submit_responses(
        self,
        poll_id: UUID,
        responses: list,
    ) -> Optional[TaskPoll]:
        """
        Submit employee responses to a poll.
        responses: [{task_id: str, new_status: str, comment: str}]
        Updates task statuses accordingly.
        """
        poll = await self.storage.get_by_id(poll_id)
        if not poll:
            return None
        if poll.status != "pending":
            raise ValueError(f"Poll is already {poll.status}")

        # Validate and apply each response
        for resp in responses:
            task_id = UUID(resp["task_id"])
            new_status = resp.get("new_status")
            if new_status:
                await self.task_service.update_status(
                    task_id=task_id,
                    new_status=new_status,
                )

        poll.responses = responses
        poll.status = "completed"
        poll.completed_at = datetime.utcnow()
        updated = await self.storage.update(poll)
        logger.info(f"Poll {poll_id} completed with {len(responses)} responses")

        return updated

    async def expire_stale_polls(self) -> List[TaskPoll]:
        """Expire polls past their expires_at. Called by scheduler."""
        now = datetime.utcnow()
        stale = await self.storage.list_pending_expired(now)
        expired = []

        for poll in stale:
            poll.status = "expired"
            await self.storage.update(poll)
            expired.append(poll)
            logger.info(f"Expired poll {poll.id} for user {poll.assignee_user_id}")

        return expired
