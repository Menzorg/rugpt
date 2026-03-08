"""
Task Poll Service

Business logic for daily morning polls.
Creates polls, processes responses, expires stale polls.
"""
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List
from uuid import UUID

from ..models.task_poll import TaskPoll
from ..storage.task_poll_storage import TaskPollStorage
from .task_service import TaskService
from .in_app_notification_service import InAppNotificationService

logger = logging.getLogger("rugpt.services.task_poll")


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

    async def create_daily_poll(
        self,
        assignee_user_id: UUID,
        org_id: UUID,
    ) -> Optional[TaskPoll]:
        """
        Create a morning poll for an employee if they have active tasks.
        Returns None if no active tasks or poll already exists for today.
        Called by scheduler (morning_poll_job).
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
        )
        created = await self.storage.create(poll)
        logger.info(f"Created daily poll for user {assignee_user_id} ({len(active_tasks)} tasks)")

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
