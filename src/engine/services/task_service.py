"""
Task Service

Business logic for employee task management.
Creates in-app notifications on task events.
"""
import logging
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from ..models.task import Task
from ..storage.task_storage import TaskStorage
from .in_app_notification_service import InAppNotificationService

logger = logging.getLogger("rugpt.services.task")

VALID_STATUSES = {"created", "in_progress", "done", "overdue"}


class TaskService:

    def __init__(
        self,
        storage: TaskStorage,
        in_app_notification_service: InAppNotificationService,
    ):
        self.storage = storage
        self.notification_service = in_app_notification_service

    async def create(
        self,
        org_id: UUID,
        title: str,
        assignee_user_id: UUID,
        description: Optional[str] = None,
        deadline: Optional[datetime] = None,
    ) -> Task:
        """Create a new task and notify the assignee"""
        if not title:
            raise ValueError("Task title is required")

        task = Task(
            org_id=org_id,
            title=title,
            description=description,
            assignee_user_id=assignee_user_id,
            deadline=deadline,
        )
        created = await self.storage.create(task)
        logger.info(f"Created task '{title}' for user {assignee_user_id} in org {org_id}")

        # Notify assignee
        await self.notification_service.create(
            user_id=assignee_user_id,
            org_id=org_id,
            type="new_task",
            title=f"Новая задача: {title}",
            content=description,
            reference_type="task",
            reference_id=created.id,
        )

        return created

    async def get(self, task_id: UUID) -> Optional[Task]:
        """Get task by ID"""
        return await self.storage.get_by_id(task_id)

    async def list_by_assignee(
        self,
        assignee_user_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List tasks assigned to a user"""
        if status and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")
        return await self.storage.list_by_assignee(assignee_user_id, status)

    async def list_by_org(
        self,
        org_id: UUID,
        status: Optional[str] = None,
    ) -> List[Task]:
        """List all tasks in an organization (for managers)"""
        if status and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")
        return await self.storage.list_by_org(org_id, status)

    async def update_status(
        self,
        task_id: UUID,
        new_status: str,
    ) -> Optional[Task]:
        """Update task status"""
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}. Must be one of {VALID_STATUSES}")

        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        old_status = task.status
        task.status = new_status
        updated = await self.storage.update(task)
        logger.info(f"Task {task_id} status: {old_status} -> {new_status}")

        return updated

    async def update(
        self,
        task_id: UUID,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        assignee_user_id: Optional[UUID] = None,
        deadline: Optional[datetime] = None,
    ) -> Optional[Task]:
        """Update task fields"""
        if status and status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of {VALID_STATUSES}")

        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if status is not None:
            task.status = status
        if assignee_user_id is not None:
            task.assignee_user_id = assignee_user_id
        if deadline is not None:
            task.deadline = deadline

        return await self.storage.update(task)

    async def deactivate(self, task_id: UUID) -> bool:
        """Soft-delete a task"""
        return await self.storage.deactivate(task_id)

    async def check_overdue(self) -> List[Task]:
        """Check for overdue tasks and update their status. Called by scheduler."""
        now = datetime.utcnow()
        tasks = await self.storage.list_active_with_deadline()
        overdue_tasks = []

        for task in tasks:
            if task.deadline and task.deadline <= now:
                task.status = "overdue"
                await self.storage.update(task)
                overdue_tasks.append(task)
                logger.info(f"Task {task.id} marked as overdue: '{task.title}'")

                # Notify assignee
                await self.notification_service.create(
                    user_id=task.assignee_user_id,
                    org_id=task.org_id,
                    type="task_status_change",
                    title=f"Задача просрочена: {task.title}",
                    reference_type="task",
                    reference_id=task.id,
                )

        return overdue_tasks
