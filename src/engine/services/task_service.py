"""
Task Service

Business logic for employee task management.
Creates in-app notifications on task events.
"""

from src.engine.unified_logger import get_logger
from datetime import date, datetime, timezone
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID

import asyncpg

from ..models.task import Task, VALID_STATUSES
from ..models.user import User
from ..storage.task_storage import TaskStorage
from ..storage.user_storage import UserStorage
from .in_app_notification_service import InAppNotificationService

if TYPE_CHECKING:
    from .chat_service import ChatService
    from .task_event_service import TaskEventService
    from .project_service import ProjectService
    from ..storage.task_participant_storage import TaskParticipantStorage

logger = get_logger("services")

class ParticipantAlreadyExists(ValueError):
    """Raised when add_participant called with a user_id already in task_participants."""
    pass

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
        user_storage: Optional[UserStorage] = None,
        task_participant_storage: Optional["TaskParticipantStorage"] = None,
    ):
        self.storage = storage
        self.notification_service = in_app_notification_service
        self.chat_service = chat_service
        self.task_event_service = task_event_service
        self.project_service = project_service
        self.user_storage = user_storage
        self.task_participant_storage = task_participant_storage

    async def _bell_to_recipients(
        self,
        task: "Task",
        actor_user_id: Optional[UUID],
        title: str,
        content: Optional[str] = None,
    ) -> None:
        """Best-effort bell notification (in_app_notifications) всем участникам
        задачи (creator + assignee + participants) кроме actor. Используется
        `_resolve_recipients` для согласованной фильтрации (active users only).
        """
        if self.notification_service is None:
            return
        try:
            recipients = await self._resolve_recipients(
                task, exclude_user_id=actor_user_id,
            )
        except Exception as e:
            logger.error(f"Bell notify resolve failed for task {task.id}: {e}")
            return
        for u in recipients:
            try:
                await self.notification_service.create(
                    user_id=u.id,
                    org_id=task.org_id,
                    type="task_status_change",
                    title=title,
                    content=content,
                    reference_type="task",
                    reference_id=task.id,
                )
            except Exception as e:
                logger.error(f"Bell notify create failed for user {u.id} task {task.id}: {e}")

    async def _bell_to_user(
        self,
        user_id: UUID,
        org_id: UUID,
        task_id: UUID,
        title: str,
        content: Optional[str] = None,
    ) -> None:
        """Best-effort single-recipient bell. Used for participant add/remove
        where notification has one specific addressee, not 'all involved'."""
        if self.notification_service is None:
            return
        try:
            await self.notification_service.create(
                user_id=user_id,
                org_id=org_id,
                type="task_status_change",
                title=title,
                content=content,
                reference_type="task",
                reference_id=task_id,
            )
        except Exception as e:
            logger.error(f"Bell single notify failed for user {user_id} task {task_id}: {e}")

    @staticmethod
    def _format_actor(user: "User") -> str:
        """Display name for actor: '@username' if username present, else plain name."""
        return f"@{user.username}" if getattr(user, "username", None) else user.name

    async def _resolve_recipients(
        self, task: "Task", exclude_user_id: Optional[UUID] = None,
    ) -> List["User"]:
        """Active users involved in the task: creator + assignee + participants.
        Deduplicates and excludes `exclude_user_id`. Filters out users with is_active=false.
        Returns [] if user_storage is unavailable (test/no-db mode).
        """
        if self.user_storage is None:
            return []
        candidate_ids: List[UUID] = []
        if task.created_by_user_id:
            candidate_ids.append(task.created_by_user_id)
        if task.assignee_user_id:
            candidate_ids.append(task.assignee_user_id)
        if self.task_participant_storage is not None:
            participant_ids = await self.task_participant_storage.list_user_ids(task.id)
            candidate_ids.extend(participant_ids)

        seen: set = set()
        unique_ids: List[UUID] = []
        for uid in candidate_ids:
            if uid is None or uid == exclude_user_id or uid in seen:
                continue
            seen.add(uid)
            unique_ids.append(uid)

        result: List["User"] = []
        for uid in unique_ids:
            user = await self.user_storage.get_by_id(uid)
            if user is None:
                continue
            if not getattr(user, "is_active", False):
                continue
            result.append(user)
        return result

    # --- Internal helpers ---

    async def _default_priority_for_creator(self, creator_id: Optional[UUID]) -> int:
        """Derive default priority from creator's role: admin=3, head=2, regular=1.
        Returns 1 if creator unknown or user_storage not wired."""
        if creator_id is None or self.user_storage is None:
            return 1
        try:
            user = await self.user_storage.get_by_id(creator_id)
        except Exception as e:
            logger.warning(f"Failed to load creator {creator_id} for priority default: {e}")
            return 1
        if user is None:
            return 1
        return compute_priority({
            "is_admin": getattr(user, "is_admin", False),
            "is_head": getattr(user, "is_head", False),
        })

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
        priority: Optional[int] = None,
        participant_user_ids: Optional[List[UUID]] = None,
    ) -> Task:
        """Create a new task, auto-create its chat, link it to a project chat, notify assignee.

        priority: 1..3. If None, derived from creator's role (admin=3, head=2, regular=1).
        """
        if not title:
            raise ValueError("Task title is required")

        if priority is not None and priority not in (1, 2, 3):
            raise ValueError("priority must be 1, 2 or 3")

        # Multi-tenancy guard: project must belong to same org and be active.
        if project_id is not None:
            if self.project_service is None:
                raise AssertionError("project_service required for project_id-aware task creation")
            project = await self.project_service.storage.get_by_id(project_id)
            if project is None or project.org_id != org_id or not project.is_active:
                raise ValueError(
                    f"Project {project_id} is not available in this organization"
                )

        # Default priority by creator role when not explicitly chosen.
        if priority is None:
            priority = await self._default_priority_for_creator(created_by_user_id)

        task = Task(
            org_id=org_id,
            title=title,
            description=description,
            assignee_user_id=assignee_user_id,
            created_by_user_id=created_by_user_id,
            deadline=deadline,
            project_id=project_id,
            priority=priority,
        )
        created = await self.storage.create(task)
        logger.info(
            f"Created task '{title}' for user {assignee_user_id} in org {org_id} "
            f"by {created_by_user_id} project={project_id}"
        )

        # 0. Persist participants (filter assignee/creator + dedupe).
        filtered_participants: List[UUID] = []
        if participant_user_ids and self.task_participant_storage is not None:
            seen = {created.assignee_user_id, created.created_by_user_id}
            for pid in participant_user_ids:
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                try:
                    await self.task_participant_storage.add(
                        task_id=created.id,
                        user_id=pid,
                        added_by_user_id=created_by_user_id,
                    )
                    filtered_participants.append(pid)
                except Exception as e:
                    logger.warning(
                        f"Failed to add participant {pid} to task {created.id}: {e}"
                    )

        # 1. Auto-create task chat. Extend membership with participants.
        if self.chat_service is not None:
            try:
                await self.chat_service.create_task_chat(
                    task_id=created.id,
                    org_id=org_id,
                    creator_id=created_by_user_id or assignee_user_id,
                    assignee_id=assignee_user_id,
                )
                for pid in filtered_participants:
                    try:
                        await self.chat_service.add_task_chat_participant(
                            created.id, pid,
                        )
                    except Exception as e:
                        logger.error(
                            f"add_task_chat_participant failed for {pid}: {e}"
                        )
            except Exception as e:
                logger.error(f"Failed to create task chat for {created.id}: {e}")

        # 2. Ensure project chat membership (lazy-creates on first task).
        if project_id and self.chat_service is not None:
            participants = [
                u for u in [created_by_user_id, assignee_user_id, *filtered_participants]
                if u
            ]
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
                "participant_user_ids": [str(p) for p in filtered_participants],
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

        # 5. Notify each new participant via bell (skip self-add).
        actor_user = None
        if self.user_storage is not None and created_by_user_id is not None:
            actor_user = await self.user_storage.get_by_id(created_by_user_id)
        actor_label = self._format_actor(actor_user) if actor_user else ""
        for pid in filtered_participants:
            if pid == created_by_user_id:
                continue
            await self._bell_to_user(
                user_id=pid,
                org_id=org_id,
                task_id=created.id,
                title=f"Вас добавили в задачу «{created.title}»",
                content=f"Добавил: {actor_label}" if actor_label else None,
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
        Tasks where user is assignee. Sorted by stored priority + deadline.
        Returns list of dicts with `task`, `creator` keys; priority is part of task.to_dict().
        """
        return await self.storage.list_by_assignee_with_priority(user_id, include_done)

    async def list_tasks_created_by(
        self,
        user_id: UUID,
        include_done: bool = False,
    ) -> List[dict]:
        """
        Tasks where user is creator. Includes assignee info.
        Returns list of dicts with `task`, `creator`, `assignee` keys.
        """
        return await self.storage.list_by_creator_with_assignee(user_id, include_done)

    async def list_archived(
        self,
        user_id: UUID,
        org_id: UUID,
        limit: int = 200,
    ) -> List[dict]:
        """
        Archived tasks for a user: done OR cancelled (is_active=false), where
        the user was creator or assignee. Same dict shape as list_tasks_created_by.
        """
        return await self.storage.list_archived_for_user(user_id, org_id, limit)

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
        priority: Optional[int] = None,
        actor_user_id: Optional[UUID] = None,
    ) -> Optional[Task]:
        """Update task fields. Status changes go through dedicated methods.

        project_id uses sentinel semantics so None can explicitly detach.
        When assignee, project_id or priority changes, chat membership is
        recomputed (assignee/project) and corresponding task_events are written.
        """
        if priority is not None and priority not in (1, 2, 3):
            raise ValueError("priority must be 1, 2 or 3")

        task = await self.storage.get_by_id(task_id)
        if not task:
            return None

        old_assignee = task.assignee_user_id
        old_project = task.project_id
        old_priority = task.priority

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
        if priority is not None:
            task.priority = priority

        updated = await self.storage.update(task)

        # Side-effects: chat membership + events.
        if assignee_user_id is not None and assignee_user_id != old_assignee:
            if self.chat_service is not None:
                await self.chat_service.add_task_chat_participant(
                    updated.id, assignee_user_id,
                )
                # Project chat: ensure new assignee is in the project chat (they may not have been
                # creator/assignee/participant of any task in this project before).
                if updated.project_id is not None:
                    try:
                        await self.chat_service.ensure_project_chat_membership(
                            project_id=updated.project_id,
                            org_id=updated.org_id,
                            user_ids=[assignee_user_id],
                        )
                    except Exception as e:
                        logger.warning(f"ensure_project_chat_membership on reassign failed: {e}")
            # Auto-swap participants on reassign:
            #   - old assignee: add to participants (unless they are creator or same as new)
            #   - new assignee: remove from participants if they were one
            # Order: add-old first so a partial failure (rare) leaves a visible duplicate
            # rather than silent data loss.
            if self.task_participant_storage is not None:
                if old_assignee and old_assignee != updated.created_by_user_id and old_assignee != assignee_user_id:
                    try:
                        await self.task_participant_storage.add(
                            task_id=updated.id, user_id=old_assignee,
                            added_by_user_id=actor_user_id,
                        )
                    except asyncpg.UniqueViolationError:
                        pass  # already a participant somehow — fine
                    except Exception as e:
                        logger.warning(f"swap-add participant failed: {e}")
                try:
                    await self.task_participant_storage.remove(updated.id, assignee_user_id)
                except Exception as e:
                    logger.warning(f"swap-remove participant failed: {e}")
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

        if priority is not None and priority != old_priority:
            await self._record_event(
                task_id=updated.id,
                actor_user_id=actor_user_id,
                event_type="priority_changed",
                payload={"from": old_priority, "to": priority},
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
    # Participants
    # ============================================

    async def add_participant(
        self, task_id: UUID, user_id: UUID, actor: User,
    ) -> dict:
        """Add a participant. Returns {id, name}. Raises:
        - PermissionError if actor is not creator/head/admin
        - ValueError if user is already creator or assignee
        - ValueError if duplicate (409 mapping at route layer)
        """
        if self.task_participant_storage is None:
            raise RuntimeError("task_participant_storage required")
        task = await self.storage.get_by_id(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator_or_head(task, actor)
        if user_id == task.assignee_user_id:
            raise ValueError("User is already assignee of this task")
        if task.created_by_user_id is not None and user_id == task.created_by_user_id:
            raise ValueError("User is already creator of this task")

        try:
            await self.task_participant_storage.add(
                task_id=task_id, user_id=user_id, added_by_user_id=actor.id,
            )
        except asyncpg.UniqueViolationError:
            raise ParticipantAlreadyExists("User is already a participant")

        # Hook: task chat
        if self.chat_service is not None:
            try:
                await self.chat_service.add_task_chat_participant(task_id, user_id)
            except Exception as e:
                logger.warning(f"add_task_chat_participant failed: {e}")

        # Hook: project chat
        if task.project_id and self.chat_service is not None:
            try:
                await self.chat_service.ensure_project_chat_membership(
                    project_id=task.project_id, org_id=task.org_id,
                    user_ids=[user_id],
                )
            except Exception as e:
                logger.warning(f"ensure_project_chat_membership failed: {e}")

        # Audit event
        await self._record_event(
            task_id=task_id, actor_user_id=actor.id,
            event_type="participant_added",
            payload={"user_id": str(user_id), "added_by": str(actor.id)},
        )

        # Notify added user via bell (skip self-add).
        if user_id != actor.id:
            await self._bell_to_user(
                user_id=user_id,
                org_id=task.org_id,
                task_id=task.id,
                title=f"Вас добавили в задачу «{task.title}»",
                content=f"Добавил: {self._format_actor(actor)}",
            )

        # Return shape for API — fetch the just-added user directly.
        if self.user_storage is not None:
            user = await self.user_storage.get_by_id(user_id)
            if user is not None:
                return {"id": user_id, "name": user.name}
        return {"id": user_id, "name": ""}

    async def remove_participant(
        self, task_id: UUID, user_id: UUID, actor: User,
    ) -> bool:
        """Remove participant. Returns False if not present (404 mapping at route layer)."""
        if self.task_participant_storage is None:
            raise RuntimeError("task_participant_storage required")
        task = await self.storage.get_by_id(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        self._check_creator_or_head(task, actor)

        removed = await self.task_participant_storage.remove(task_id, user_id)
        if not removed:
            return False

        # Hook: task chat — remove from chat only if user is not creator/assignee.
        if self.chat_service is not None and user_id not in {
            task.created_by_user_id, task.assignee_user_id,
        }:
            try:
                chat = await self.chat_service.get_task_chat(task_id)
                if chat is not None:
                    await self.chat_service.remove_participant(chat.id, user_id)
            except Exception as e:
                logger.warning(f"remove from task chat failed: {e}")

        # Hook: project chat — remove only if user has no other active task in project.
        if task.project_id and self.chat_service is not None:
            try:
                still_in = await self.storage.user_has_any_active_task_in_project(
                    task.project_id, user_id,
                )
                if not still_in:
                    chat = await self.chat_service.chat_storage.get_by_project_id(
                        task.project_id,
                    )
                    if chat is not None:
                        await self.chat_service.remove_participant(chat.id, user_id)
            except Exception as e:
                logger.warning(f"remove from project chat failed: {e}")

        # Audit
        await self._record_event(
            task_id=task_id, actor_user_id=actor.id,
            event_type="participant_removed",
            payload={"user_id": str(user_id), "removed_by": str(actor.id)},
        )

        # Notify removed user via bell (skip self-remove).
        if user_id != actor.id:
            await self._bell_to_user(
                user_id=user_id,
                org_id=task.org_id,
                task_id=task.id,
                title=f"Вас исключили из задачи «{task.title}»",
                content=f"Исключил: {self._format_actor(actor)}",
            )

        return True

    async def list_participating(
        self, user_id: UUID, include_done: bool = False,
    ) -> List[dict]:
        """Tasks where user is in task_participants. Same shape as list_my_tasks."""
        return await self.storage.list_by_participant_with_priority(
            user_id, include_done,
        )

    async def list_done(self, user_id: UUID) -> List[dict]:
        """Done tasks where user is creator OR assignee OR participant."""
        return await self.storage.list_done_for_user(user_id)

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

    def _check_creator_or_head(self, task: Task, user: User):
        """Allows: creator (if task has one) OR head OR admin."""
        if user.is_admin or getattr(user, "is_head", False):
            return
        if task.created_by_user_id is not None and task.created_by_user_id == user.id:
            return
        raise PermissionError("Only creator, head, or admin can perform this action")

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
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» взята в работу",
            content=self._format_actor(user),
        )
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
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» отмечена выполненной",
            content=self._format_actor(user),
        )
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
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» принята",
        )
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
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Задача «{updated.title}» возвращена в работу",
            content=comment,
        )
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
        is_self_assigned = task.created_by_user_id == task.assignee_user_id == user.id
        if task.status == "done" or (task.status == "overdue" and not is_self_assigned):
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
        deadline_str = updated.deadline.strftime("%d.%m.%Y %H:%M") if updated.deadline else "—"
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Срок задачи «{updated.title}» изменён",
            content=f"Новый срок: {deadline_str}",
        )
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
        proposed_str = (
            updated.proposed_deadline.strftime("%d.%m.%Y %H:%M")
            if updated.proposed_deadline else "—"
        )
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Предложен новый срок для «{updated.title}»",
            content=f"{self._format_actor(user)}: {proposed_str}",
        )
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
        deadline_str = updated.deadline.strftime("%d.%m.%Y %H:%M") if updated.deadline else "—"
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Новый срок для «{updated.title}» принят",
            content=deadline_str,
        )
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
        await self._bell_to_recipients(
            updated, user.id,
            title=f"Новый срок для «{updated.title}» отклонён",
        )
        return updated

    # ============================================
    # Scheduler
    # ============================================

    async def check_overdue(self) -> List[Task]:
        """Check for overdue tasks and update their status. Called by scheduler."""
        # task.deadline приходит из timestamptz (aware), поэтому сравниваем aware-aware.
        now = datetime.now(timezone.utc)
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
                # Bell всем involved (creator + assignee + participants).
                # actor_user_id=None — overdue scheduler-driven, исключать некого.
                await self._bell_to_recipients(
                    task, actor_user_id=None,
                    title=f"Задача просрочена: {task.title}",
                )

        return overdue_tasks
