"""
Task Service

Business logic for employee task management.
Creates in-app notifications on task events.
"""
import logging
from datetime import date, datetime
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID

from ..models.task import Task, VALID_STATUSES
from ..models.user import User
from ..storage.task_storage import TaskStorage
from .in_app_notification_service import InAppNotificationService

if TYPE_CHECKING:
    from .chat_service import ChatService
    from .task_event_service import TaskEventService
    from .project_service import ProjectService
    from .task_notification_service import TaskNotificationService

logger = logging.getLogger("rugpt.services.task")


def compute_priority(creator: Optional[dict]) -> int:
    """
    Priority by task creator's role:
    3 = admin (org owner)
    2 = head (department head)
    1 = regular user (also default for legacy tasks without creator)
    """
    if creator is None:
        return 1
    if creator.get("is_admin"):
        return 3
    if creator.get("is_head"):
        return 2
    return 1


class TaskService:

    def __init__(
        self,
        storage: TaskStorage,
        in_app_notification_service: InAppNotificationService,
        chat_service: Optional["ChatService"] = None,
        task_event_service: Optional["TaskEventService"] = None,
        project_service: Optional["ProjectService"] = None,
        task_notification_service: Optional["TaskNotificationService"] = None,
    ):
        self.storage = storage
        self.notification_service = in_app_notification_service
        self.chat_service = chat_service
        self.task_event_service = task_event_service
        self.project_service = project_service
        self.task_notification_service = task_notification_service

    async def _notify(self, method_name: str, *args, **kwargs) -> None:
        """Best-effort PM notification via TaskNotificationService. Silent no-op if absent."""
        if self.task_notification_service is None:
            return
        try:
            method = getattr(self.task_notification_service, method_name)
            await method(*args, **kwargs)
        except Exception as e:
            logger.error(f"PM notify {method_name} failed: {e}")

    # --- Internal helpers ---

    async def _record_event(
        self,
        task_id: UUID,
        actor_user_id: Optional[UUID],
        event_type: str,
        payload: Optional[dict] = None,
    ) -> None:
        """Best-effort event record. Silently no-op if task_event_service not wired."""
        if self.task_event_service is None:
            return
        await self.task_event_service.record(
            task_id=task_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            payload=payload or {},
        )

    async def create(
        self,
        org_id: UUID,
        title: str,
        assignee_user_id: UUID,
        description: Optional[str] = None,
        deadline: Optional[datetime] = None,
        created_by_user_id: Optional[UUID] = None,
        project_id: Optional[UUID] = None,
    ) -> Task:
        """Create a new task, auto-create its chat, link it to a project chat, notify assignee."""
        if not title:
            raise ValueError("Task title is required")

        # Multi-tenancy guard: project must belong to same org and be active.
        if project_id is not None:
            if self.project_service is None:
                raise AssertionError("project_service required for project_id-aware task creation")
            project = await self.project_service.storage.get_by_id(project_id)
            if project is None or project.org_id != org_id or not project.is_active:
                raise ValueError(
                    f"Project {project_id} is not available in this organization"
                )

        task = Task(
            org_id=org_id,
            title=title,
            description=description,
            assignee_user_id=assignee_user_id,
            created_by_user_id=created_by_user_id,
            deadline=deadline,
            project_id=project_id,
        )
        created = await self.storage.create(task)
        logger.info(
            f"Created task '{title}' for user {assignee_user_id} in org {org_id} "
            f"by {created_by_user_id} project={project_id}"
        )

        # 1. Auto-create task chat.
        if self.chat_service is not None:
            try:
                await self.chat_service.create_task_chat(
                    task_id=created.id,
                    org_id=org_id,
                    creator_id=created_by_user_id or assignee_user_id,
                    assignee_id=assignee_user_id,
                )
            except Exception as e:
                logger.error(f"Failed to create task chat for {created.id}: {e}")

        # 2. Ensure project chat membership (lazy-creates on first task).
        if project_id and self.chat_service is not None:
            participants = [u for u in [created_by_user_id, assignee_user_id] if u]
            try:
                await self.chat_service.ensure_project_chat_membership(
                    project_id=project_id,
                    org_id=org_id,
                    user_ids=participants,
                )
            except Exception as e:
                logger.error(f"Failed to ensure project chat for {project_id}: {e}")

        # 3. Audit event.
        await self._record_event(
            task_id=created.id,
            actor_user_id=created_by_user_id,
            event_type="created",
            payload={
                "title": title,
                "assignee_user_id": str(assignee_user_id),
                "project_id": str(project_id) if project_id else None,
            },
        )

        # 4. Notify assignee (existing behaviour).
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
        """List tasks assigned to a user (legacy method, used by scheduler)"""
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

    async def text_search(
        self,
        org_id: UUID,
        query: str,
        limit: int = 80,
    ) -> List[Task]:
        """Full-text search over task title (A) and description (C) using ts_rank_cd."""
        return await self.storage.text_search(org_id, query, limit)

    async def list_by_deadline_range(
        self,
        org_id: UUID,
        deadline_from: Optional[date] = None,
        deadline_to: Optional[date] = None,
    ) -> List[Task]:
        """List active tasks whose deadline falls within an inclusive date interval."""
        return await self.storage.list_by_deadline_range(
            org_id, deadline_from=deadline_from, deadline_to=deadline_to,
        )

    async def list_by_created_range(
        self,
        org_id: UUID,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
    ) -> List[Task]:
        """List active tasks whose creation date falls within an inclusive date interval."""
        return await self.storage.list_by_created_range(
            org_id, created_from=created_from, created_to=created_to,
        )

    async def list_my_tasks(
        self,
        user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is assignee. Sorted by priority (creator role) + deadline.
        Returns list of dicts with `task`, `creator`, `priority` keys.
        """
        rows = await self.storage.list_by_assignee_with_priority(user_id, include_done)
        for entry in rows:
            entry["priority"] = compute_priority(entry["creator"])
        return rows

    async def list_tasks_created_by(
        self,
        user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is creator. Includes assignee info.
        Returns list of dicts with `task`, `creator`, `assignee`, `priority` keys.
        """
        rows = await self.storage.list_by_creator_with_assignee(user_id, include_done)
        for entry in rows:
            entry["priority"] = compute_priority(entry["creator"])
        return rows

    async def list_archived(
        self,
        user_id: UUID,
        org_id: UUID,
        limit: int = 200,
    ) -> List[dict]:
        """
        Archived tasks for a user: done OR cancelled (is_active=false), where
        the user was creator or assignee. Same dict shape as list_tasks_created_by
        (`task`, `creator`, `assignee`, `priority`).
        """
        rows = await self.storage.list_archived_for_user(user_id, org_id, limit)
        for entry in rows:
            entry["priority"] = compute_priority(entry["creator"])
        return rows

    async def update_status(
        self,
        task_id: UUID,
        new_status: str,
    ) -> Optional[Task]:
        """Update task status (legacy, used by scheduler for overdue)"""
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

    _UNSET = object()

    async def update(
        self,
        task_id: UUID,
        title: Optional[str] = None,
        description: Optional[str] = None,
        assignee_user_id: Optional[UUID] = None,
        deadline: Optional[datetime] = None,
        project_id=_UNSET,  # sentinel: _UNSET = no change, None = detach, UUID = set
        actor_user_id: Optional[UUID] = None,
    ) -> Optional[Task]:
        """Update task fields. Status changes go through dedicated methods.

        project_id uses sentinel semantics so None can explicitly detach.
        When assignee or project_id changes, chat membership is recomputed
        and corresponding task_events are written.
        """
        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        old_assignee = task.assignee_user_id
        old_project = task.project_id

        if title is not None:
            task.title = title
        if description is not None:
            task.description = description
        if assignee_user_id is not None:
            task.assignee_user_id = assignee_user_id
        if deadline is not None:
            task.deadline = deadline
        if project_id is not TaskService._UNSET:
            # Guard cross-org project reassignment.
            if project_id is not None and self.project_service is not None:
                project = await self.project_service.storage.get_by_id(project_id)
                if project is None or project.org_id != task.org_id or not project.is_active:
                    raise ValueError(
                        f"Project {project_id} is not available in this organization"
                    )
            task.project_id = project_id

        updated = await self.storage.update(task)

        # Side-effects: chat membership + events.
        if assignee_user_id is not None and assignee_user_id != old_assignee:
            if self.chat_service is not None:
                await self.chat_service.add_task_chat_participant(
                    updated.id, assignee_user_id,
                )
            await self._record_event(
                task_id=updated.id,
                actor_user_id=actor_user_id,
                event_type="assignee_changed",
                payload={
                    "from": str(old_assignee) if old_assignee else None,
                    "to": str(assignee_user_id),
                },
            )

        if project_id is not TaskService._UNSET and project_id != old_project:
            if project_id and self.chat_service is not None:
                participants = [u for u in [
                    updated.created_by_user_id, updated.assignee_user_id,
                ] if u]
                await self.chat_service.ensure_project_chat_membership(
                    project_id=project_id,
                    org_id=updated.org_id,
                    user_ids=participants,
                )
            await self._record_event(
                task_id=updated.id,
                actor_user_id=actor_user_id,
                event_type="project_changed",
                payload={
                    "from": str(old_project) if old_project else None,
                    "to": str(project_id) if project_id else None,
                },
            )

        return updated

    async def deactivate(
        self,
        task_id: UUID,
        user: Optional[User] = None,
    ) -> bool:
        """Soft-delete a task. Archives its chat and, if it was the last active
        task in its project, archives the project chat as well. Writes a 'cancelled' event.

        `user` is optional for backwards compatibility with legacy callers; when
        present, its id is used as actor_user_id in the audit event.
        """
        task = await self.storage.get_by_id(task_id)
        if not task:
            return False
        if user is not None and task.org_id != user.org_id:
            return False

        ok = await self.storage.deactivate(task_id)
        if not ok:
            return False

        if self.chat_service is not None:
            try:
                await self.chat_service.archive_task_chat(task_id)
            except Exception as e:
                logger.error(f"Failed to archive task chat {task_id}: {e}")

        await self._record_event(
            task_id=task_id,
            actor_user_id=user.id if user else None,
            event_type="cancelled",
            payload={},
        )

        if task.project_id and self.chat_service is not None:
            try:
                remaining = await self.storage.count_active_in_project(task.project_id)
                if remaining == 0:
                    await self.chat_service.archive_project_chat(task.project_id)
            except Exception as e:
                logger.error(
                    f"Failed to check/archive project chat for {task.project_id}: {e}"
                )

        return True

    # ============================================
    # Permission helpers
    # ============================================

    def _check_assignee(self, task: Task, user: User):
        if task.assignee_user_id != user.id:
            raise PermissionError("Only assignee can perform this action")

    def _check_creator(self, task: Task, user: User):
        if task.created_by_user_id is not None:
            if task.created_by_user_id != user.id and not user.is_admin:
                raise PermissionError("Only task creator can perform this action")
        else:
            # Legacy task: only admin can manage
            if not user.is_admin:
                raise PermissionError("Only admin can manage legacy tasks without creator")

    # ============================================
    # Status transitions
    # ============================================

    async def take_task(self, task_id: UUID, user: User) -> Task:
        """Assignee takes task: created -> in_progress."""
        logger.info(f"take_task: task={task_id} user={user.id}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        if task.status != "created":
            raise ValueError(f"Cannot take task in status '{task.status}'")
        old_status = task.status
        task.status = "in_progress"
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="took",
            payload={"from_status": old_status, "to_status": "in_progress"},
        )
        await self._notify("notify_take", updated, user)
        return updated

    async def mark_done(self, task_id: UUID, user: User) -> Task:
        """Assignee marks task done: in_progress -> awaiting_review."""
        logger.info(f"mark_done: task={task_id} user={user.id}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        if task.status != "in_progress":
            raise ValueError(f"Cannot mark done from status '{task.status}'")
        task.status = "awaiting_review"
        task.awaiting_review_at = datetime.utcnow()
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="marked_done",
            payload={"from_status": "in_progress", "to_status": "awaiting_review"},
        )
        await self._notify("notify_mark_done", updated, user)
        return updated

    async def accept_task(self, task_id: UUID, user: User) -> Task:
        """Creator accepts: awaiting_review -> done."""
        logger.info(f"accept_task: task={task_id} user={user.id}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.status != "awaiting_review":
            raise ValueError(f"Cannot accept from status '{task.status}'")
        task.status = "done"
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="accepted",
            payload={"from_status": "awaiting_review", "to_status": "done"},
        )
        await self._notify("notify_accept", updated, user)
        return updated

    async def reject_task(
        self, task_id: UUID, user: User, comment: Optional[str] = None,
    ) -> Task:
        """Creator rejects: awaiting_review -> in_progress."""
        logger.info(f"reject_task: task={task_id} user={user.id} has_comment={bool(comment)}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.status != "awaiting_review":
            raise ValueError(f"Cannot reject from status '{task.status}'")
        task.status = "in_progress"
        task.awaiting_review_at = None
        if comment:
            logger.info(f"Task {task_id} rejected by {user.id} with comment: {comment}")
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="rejected",
            payload={
                "from_status": "awaiting_review",
                "to_status": "in_progress",
                "comment": comment,
            },
        )
        await self._notify("notify_reject", updated, user, comment)
        return updated

    # ============================================
    # Deadline negotiation
    # ============================================

    async def set_deadline(
        self, task_id: UUID, user: User, deadline: datetime,
    ) -> Task:
        """Creator changes deadline directly."""
        logger.info(f"set_deadline: task={task_id} user={user.id} deadline={deadline.isoformat()}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.status in ("done", "overdue"):
            raise ValueError(f"Cannot set deadline on task in status '{task.status}'")
        old_deadline = task.deadline
        task.deadline = deadline
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="deadline_set",
            payload={
                "old": old_deadline.isoformat() if old_deadline else None,
                "new": deadline.isoformat(),
            },
        )
        await self._notify("notify_set_deadline", updated, user)
        return updated

    async def propose_deadline(
        self, task_id: UUID, user: User, proposed: datetime,
    ) -> Task:
        """Assignee proposes alternative deadline."""
        logger.info(f"propose_deadline: task={task_id} user={user.id} proposed={proposed.isoformat()}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_assignee(task, user)
        if task.status not in ("created", "in_progress"):
            raise ValueError(f"Cannot propose deadline in status '{task.status}'")
        task.proposed_deadline = proposed
        task.proposed_deadline_by = user.id
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="deadline_proposed",
            payload={"proposed": proposed.isoformat()},
        )
        await self._notify("notify_propose_deadline", updated, user)
        return updated

    async def accept_proposed_deadline(self, task_id: UUID, user: User) -> Task:
        """Creator accepts assignee's deadline proposal."""
        logger.info(f"accept_proposed_deadline: task={task_id} user={user.id}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.proposed_deadline is None:
            raise ValueError("No pending deadline proposal")
        old_deadline = task.deadline
        proposed = task.proposed_deadline
        task.deadline = proposed
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="deadline_proposal_accepted",
            payload={
                "old": old_deadline.isoformat() if old_deadline else None,
                "new": proposed.isoformat(),
            },
        )
        await self._notify("notify_accept_proposed_deadline", updated, user)
        return updated

    async def reject_proposed_deadline(self, task_id: UUID, user: User) -> Task:
        """Creator rejects assignee's deadline proposal."""
        logger.info(f"reject_proposed_deadline: task={task_id} user={user.id}")
        task = await self.storage.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator(task, user)
        if task.proposed_deadline is None:
            raise ValueError("No pending deadline proposal")
        rejected = task.proposed_deadline
        task.proposed_deadline = None
        task.proposed_deadline_by = None
        updated = await self.storage.update(task)
        await self._record_event(
            task_id=task_id, actor_user_id=user.id,
            event_type="deadline_proposal_rejected",
            payload={"rejected": rejected.isoformat() if rejected else None},
        )
        await self._notify("notify_reject_proposed_deadline", updated, user)
        return updated

    # ============================================
    # Scheduler
    # ============================================

    async def check_overdue(self) -> List[Task]:
        """Check for overdue tasks and update their status. Called by scheduler."""
        now = datetime.utcnow()
        tasks = await self.storage.list_active_with_deadline()
        logger.info(f"check_overdue: scanning {len(tasks)} tasks with deadline")
        overdue_tasks = []

        for task in tasks:
            if task.deadline and task.deadline <= now:
                old_status = task.status
                task.status = "overdue"
                await self.storage.update(task)
                overdue_tasks.append(task)
                logger.info(f"Task {task.id} marked as overdue: '{task.title}'")

                await self._record_event(
                    task_id=task.id,
                    actor_user_id=None,  # scheduler-driven
                    event_type="overdue",
                    payload={"from_status": old_status},
                )
                await self._notify("notify_overdue", task)

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
