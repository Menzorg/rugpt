"""
Task Tools

LangChain tools for task management (create, query, update).
Uses factory function to inject TaskService dependency.

Tools are async — invoked directly in the running event loop (the same one
that owns the asyncpg pool), so we just `await` service calls. No threading,
no nested asyncio.run(). This avoids the `There is no current event loop`
errors that the sync-wrapper approach produced under langchain-openai.
"""

from src.engine.unified_logger import get_logger

from datetime import date, datetime, timezone as dt_timezone
from typing import List, Literal, Optional
from uuid import UUID
import zoneinfo

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ...config import Config
from ...services.task_service import TaskService

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

# ============================================
# Tool input schemas
# ============================================

class TaskCreateInput(BaseModel):
    title: str = Field(description="Task title")
    description: str = Field(default="", description="Task description")
    assignee_user_id: str = Field(description="UUID of the employee to assign the task to")
    deadline: str = Field(default="", description="Deadline in ISO format (e.g. 2025-03-15T18:00:00)")
    participant_user_ids: Optional[List[str]] = Field(default=None, description="Optional UUIDs of additional task participants")


_TASKS_PAGE_SIZE = 50

class TaskQueryInput(BaseModel):
    assignee_user_id: str = Field(default="", description="Filter by UUID of employee the task is assigned to (empty = any)")
    created_by_user_id: str = Field(default="", description="Filter by UUID of the user who CREATED the task (empty = any)")
    status: Literal["done", "created", "in_progress"] | None = Field(default=None, description="Filter by status: created, in_progress, done, overdue (empty = all)")
    text_search_query: str = Field(default="", description="Full-text search over task title and description (empty = skip)")
    deadline_from: Optional[date] = Field(default=None, description="Filter tasks with deadline on or after this date, YYYY-MM-DD (empty = no lower bound)")
    deadline_to: Optional[date] = Field(default=None, description="Filter tasks with deadline on or before this date, YYYY-MM-DD (empty = no upper bound)")
    created_from: Optional[date] = Field(default=None, description="Filter tasks created on or after this date, YYYY-MM-DD (empty = no lower bound)")
    created_to: Optional[date] = Field(default=None, description="Filter tasks created on or before this date, YYYY-MM-DD (empty = no upper bound)")
    page: int = Field(default=1, description="Page number (default 1, page size is 50).")


class TaskDeadlineProposalInput(BaseModel):
    task_id: str = Field(description="UUID of the task whose proposed deadline to accept or reject")
    accept: bool = Field(description="True to accept the proposed deadline, False to reject it")


class GetOwnTasksInput(BaseModel):
    status: Literal["done", "created", "in_progress"] | None = Field(
        default=None,
        description="Filter by status: created, in_progress, done (empty = all active tasks, excluding done and overdue older than 30 days)",
    )
    page: int = Field(default=1, description="Page number (default 1, page size is 50)")


class TaskUpdateInput(BaseModel):
    task_id: str = Field(description="UUID of the task to update")
    status: str = Field(default="", description="New status: created, in_progress, awaiting_review, done. Transitions are role-restricted — the tool will return an error if the caller is not permitted.")
    title: str = Field(default="", description="New title (empty = keep current)")
    description: str = Field(default="", description="New description (empty = keep current)")
    deadline: Optional[str] = Field(default=None, description="New deadline in ISO format (e.g. 2025-03-15T18:00:00). Only the task creator or admin can set this.")
    priority: Optional[int] = Field(default=None, description="New task priority: 1 (Обычно), 2 (Важно), 3 (Срочно). Only the task creator can set this.")
    new_participant_user_ids: Optional[List[str]] = Field(default=None, description="Optional UUIDs of task participants to add")
    delete_participant_user_ids: Optional[List[str]] = Field(default=None, description="Optional UUIDs of task participants to remove")


def _check_status_transition(
    current: str,
    new: str,
    is_creator: bool,
    is_assignee: bool,
) -> Optional[str]:
    if new == current:
        return f"Task is already in status '{new}'. No change needed."
    if current == "done":
        return "Task is already done and cannot be changed."
    if new == "awaiting_review" and not is_assignee:
        return "Only the task assignee can set status to 'awaiting_review'. "
    if current == "in_progress" and new == "done":
        return "You can't change status directly from 'in_progress' to 'done'. Assignee must set it to 'awaiting_review' first."
    if current == "awaiting_review":
        if not is_creator:
            return (
                "Only the task creator can act on a task in 'awaiting_review' status. "
                "You are not the creator of this task."
            )
        if new not in ("in_progress", "done"):
            return (
                f"Cannot change status from 'awaiting_review' to '{new}'. "
                "Allowed transitions: 'in_progress' (send back for rework) or 'done' (accept)."
            )
    elif current == "created":
        if not is_assignee:
            return (
                "Only the task assignee can update the status of a task in 'created' status. "
                "You are not the assignee of this task."
            )
        if new != "in_progress":
            return (
                f"Cannot change status from 'created' to '{new}'. "
                "The only allowed transition is to 'in_progress'."
            )
    return None


# ============================================
# Factory: create tools wired to TaskService
# ============================================

def create_task_tools(
    task_service: TaskService,
):
    """
    Create task tools wired to a real TaskService instance.

    Returns (task_create_tool, task_query_tool, task_update_tool, task_deadline_proposal_tool).
    """

    def _parse_deadline(deadline: str, tz_name: str) -> Optional[datetime]:
        """Parse ISO deadline string, treating naive datetimes as org-local time, returning UTC-aware."""
        if not deadline:
            return None
        dt = datetime.fromisoformat(deadline)
        if dt.tzinfo is None:
            tz = zoneinfo.ZoneInfo(tz_name)
            dt = dt.replace(tzinfo=tz).astimezone(dt_timezone.utc)
        return dt

    def _parse_uuid_list(values: Optional[List[str]]) -> List[UUID]:
        """Parse optional UUID list args from LangChain/Pydantic."""
        if values is None:
            return []
        return [UUID(v) for v in values if v]

    def _log_identity(tool_name: str, configurable: dict) -> None:
        logger.info(
            "%s identity: caller_user_id=%s callee_user_id=%s org_id=%s is_admin=%s invocation=%s",
            tool_name,
            configurable.get("caller_user_id", ""),
            configurable.get("callee_user_id", ""),
            configurable.get("org_id", ""),
            bool(configurable.get("is_admin", False)),
            configurable.get("invocation_kind", ""),
        )

    async def _task_create_async(
        title: str,
        assignee_user_id: str,
        deadline: Optional[str] = "",
        description: Optional[str] = "",
        participant_user_ids: Optional[List[str]] = None,
        priority: Optional[int] = None,
        config: RunnableConfig = None,
    ) -> str:
        """Create a task for an employee. Use when a manager assigns work via chat.
        Args:
            title: Task title
            assignee_user_id: UUID of the employee
            description: Task description
            deadline: Deadline in ISO format
            priority: Task priority — 1 (Обычно), 2 (Важно), 3 (Срочно).
                ALWAYS ask the user which priority they want before creating the task.
                Do not guess. If the user has not specified, ask explicitly.
            participant_user_ids: Optional UUIDs of additional task participants.
        """
        logger.info(
            "tool task_create: title=%r description_chars=%d assignee=%s deadline=%r priority=%s participants=%s",
            title,
            len(description or ""),
            assignee_user_id,
            deadline,
            priority,
            participant_user_ids,
        )
        try:
            configurable = (config or {}).get("configurable", {})
            _log_identity("task_create", configurable)
            caller_uuid = UUID(configurable["caller_user_id"])
            task_org_id = UUID(configurable["org_id"])
            org_tz = configurable.get("timezone", "Europe/Moscow")

            assignee_uuid = UUID(assignee_user_id)
            participant_uuids = _parse_uuid_list(participant_user_ids)
            try:
                dl = _parse_deadline(deadline, org_tz)
            except ValueError:
                logger.info("task_create invalid_deadline: deadline=%r", deadline)
                return f"Invalid deadline format: {deadline!r}. Use ISO format, e.g. '2025-03-15T18:00:00'."

            # Refuse if the caller's department visibility rules don't include the assignee.
            from ...services.engine_service import get_engine_service
            engine = get_engine_service()
            visible = await engine.department_service.check_visible(
                caller_uuid, assignee_uuid, task_org_id,
            )
            logger.info(
                "task_create permission: gate=assignee_visible assignee=%s visible=%s",
                assignee_uuid,
                visible,
            )
            if not visible:
                return "Cannot assign task: user not visible to you."
            for participant_uuid in participant_uuids:
                participant_visible = await engine.department_service.check_visible(
                    caller_uuid, participant_uuid, task_org_id,
                )
                logger.info(
                    "task_create permission: gate=participant_visible participant=%s visible=%s",
                    participant_uuid,
                    participant_visible,
                )
                if not participant_visible:
                    return "Cannot add task participant: user not visible to you."

            if priority is not None and priority not in (1, 2, 3):
                logger.info("task_create invalid_priority: priority=%s", priority)
                return "priority must be 1 (Обычно), 2 (Важно) or 3 (Срочно)"

            task = await task_service.create(
                org_id=task_org_id,
                title=title,
                description=description or None,
                assignee_user_id=assignee_uuid,
                deadline=dl,
                created_by_user_id=caller_uuid,
                priority=priority,
                participant_user_ids=participant_uuids,
            )
            logger.info(
                "task_create done: task_id=%s status=%s assignee=%s participants=%d",
                task.id,
                task.status,
                assignee_uuid,
                len(participant_uuids),
            )
            return f"Task '{title}' created (id={task.id})"
        except Exception as e:
            logger.error(f"task_create failed: {e}", exc_info=True)
            if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
                return f"Invalid UUID in input: {e}"
            return _TOOL_ERROR_RESULT

    async def _task_query_async(
        assignee_user_id: Optional[str] = "",
        created_by_user_id: Optional[str] = "",
        status: Optional[Literal["done", "created", "in_progress"]] = "",
        text_search_query: Optional[str] = "",
        deadline_from: Optional[date] = None,
        deadline_to: Optional[date] = None,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
        page: int = 1,
        config: RunnableConfig = None,
    ) -> str:
        """Query tasks. Filter by assignee, creator, status, full-text search, and/or date ranges.
        Args:
            assignee_user_id: UUID of assignee (empty = any)
            created_by_user_id: UUID of creator (empty = any)
            status: Filter by status (empty = all)
            text_search_query: Full-text search over title and description (empty = skip)
            deadline_from: Include tasks with deadline on or after this date (None = no lower bound)
            deadline_to: Include tasks with deadline on or before this date (None = no upper bound)
            created_from: Include tasks created on or after this date (None = no lower bound)
            created_to: Include tasks created on or before this date (None = no upper bound)
        """
        logger.info(
            f"tool task_query: assignee={assignee_user_id!r} creator={created_by_user_id!r}"
            f" status={status!r} text_search={text_search_query!r}"
            f" deadline={deadline_from}..{deadline_to} created={created_from}..{created_to}"
        )
        try:
            configurable = (config or {}).get("configurable", {})
            _log_identity("task_query", configurable)
            caller_uuid = UUID(configurable["caller_user_id"])
            query_org_id = UUID(configurable["org_id"])
            org_tz = zoneinfo.ZoneInfo(configurable.get("timezone", "Europe/Moscow"))

            from ...services.engine_service import get_engine_service
            engine = get_engine_service()

            # Each active filter independently fetches its candidate pool and
            # records which task UUIDs it matched. The final set is the
            # intersection of all non-empty filter sets, so only tasks that
            # satisfy every supplied filter are shown.
            filter_sets: list[set] = []
            pool: list = []  # ordered union of all candidate tasks (first-seen wins)
            seen: set = set()

            def _add_to_pool(tasks):
                for t in tasks:
                    if t.id not in seen:
                        seen.add(t.id)
                        pool.append(t)

            if assignee_user_id:
                assignee_tasks = await task_service.list_by_assignee(
                    UUID(assignee_user_id), status or None,
                )
                filter_sets.append({t.id for t in assignee_tasks})
                _add_to_pool(assignee_tasks)
                logger.info("task_query filter: kind=assignee assignee=%s status=%s count=%d pool=%d",
                            assignee_user_id, status, len(assignee_tasks), len(pool))

            if created_by_user_id:
                rows = await task_service.list_tasks_created_by(UUID(created_by_user_id))
                creator_tasks = [r["task"] for r in rows]
                filter_sets.append({t.id for t in creator_tasks})
                _add_to_pool(creator_tasks)
                logger.info("task_query filter: kind=creator creator=%s count=%d pool=%d",
                            created_by_user_id, len(creator_tasks), len(pool))

            if status and not assignee_user_id:
                # status is already passed into list_by_assignee above; only
                # fetch by status separately when no assignee filter was given.
                status_tasks = await task_service.list_by_org(query_org_id, status)
                filter_sets.append({t.id for t in status_tasks})
                _add_to_pool(status_tasks)
                logger.info("task_query filter: kind=status status=%s count=%d pool=%d",
                            status, len(status_tasks), len(pool))

            if text_search_query:
                search_tasks = await task_service.text_search(query_org_id, text_search_query)
                filter_sets.append({t.id for t in search_tasks})
                _add_to_pool(search_tasks)
                logger.info("task_query filter: kind=text_search query=%r count=%d pool=%d",
                            text_search_query, len(search_tasks), len(pool))

            if deadline_from or deadline_to:
                deadline_tasks = await task_service.list_by_deadline_range(
                    query_org_id, deadline_from=deadline_from, deadline_to=deadline_to,
                )
                filter_sets.append({t.id for t in deadline_tasks})
                _add_to_pool(deadline_tasks)
                logger.info("task_query filter: kind=deadline from=%s to=%s count=%d pool=%d",
                            deadline_from, deadline_to, len(deadline_tasks), len(pool))

            if created_from or created_to:
                created_tasks = await task_service.list_by_created_range(
                    query_org_id, created_from=created_from, created_to=created_to,
                )
                filter_sets.append({t.id for t in created_tasks})
                _add_to_pool(created_tasks)
                logger.info("task_query filter: kind=created from=%s to=%s count=%d pool=%d",
                            created_from, created_to, len(created_tasks), len(pool))

            # No filter at all → return all org tasks
            if not filter_sets:
                all_tasks = await task_service.list_by_org(query_org_id, status or None)
                _add_to_pool(all_tasks)
                final_ids = {t.id for t in pool}
                logger.info("task_query filter: kind=all_org status=%s count=%d pool=%d",
                            status, len(all_tasks), len(pool))
            else:
                final_ids = set.intersection(*filter_sets)

            tasks = [t for t in pool if t.id in final_ids]
            before_visibility_count = len(tasks)
            logger.info("task_query intersection: filters=%d pool=%d final=%d",
                        len(filter_sets), len(pool), before_visibility_count)

            # Strip tasks whose assignees are outside the caller's department visibility.
            visible_ids = await engine.department_service.get_visible_user_ids(
                caller_uuid, query_org_id,
            )
            tasks = [t for t in tasks if t.assignee_user_id in visible_ids]
            logger.info("task_query visibility: visible_users=%d before=%d after=%d",
                        len(visible_ids), before_visibility_count, len(tasks))

            if not tasks:
                logger.info("task_query done: result=empty before_visibility=%d", before_visibility_count)
                return "No tasks found."

            total = len(tasks)
            page = max(1, page)
            total_pages = max(1, (total + _TASKS_PAGE_SIZE - 1) // _TASKS_PAGE_SIZE)
            page = min(page, total_pages)
            start = (page - 1) * _TASKS_PAGE_SIZE
            end = min(start + _TASKS_PAGE_SIZE, total)
            shown = tasks[start:end]

            # TODO: replace char budget with token budget via a token counting service
            total_desc_chars = sum(len(t.description) for t in shown if t.description)
            include_descriptions = total_desc_chars <= Config.TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET
            logger.info(
                "task_query page: page=%d/%d span=%d-%d total=%d shown=%d desc_chars=%d desc_budget=%d include_desc=%s",
                page,
                total_pages,
                start + 1,
                end,
                total,
                len(shown),
                total_desc_chars,
                Config.TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET,
                include_descriptions,
            )

            # Resolve all referenced user IDs to names in one batch query.
            from ...services.engine_service import get_engine_service
            engine = get_engine_service()
            user_ids = {t.assignee_user_id for t in shown if t.assignee_user_id}
            user_ids |= {t.created_by_user_id for t in shown if t.created_by_user_id}
            user_ids |= {t.proposed_deadline_by for t in shown if t.proposed_deadline_by}
            users = await engine.user_storage.get_certain_users(list(user_ids))
            name_map = {u.id: u.name for u in users}
            participants_by_task = await engine.task_participant_storage.get_for_tasks(
                [t.id for t in shown],
            )

            lines = []
            for t in shown:
                dl = f", deadline: {t.deadline.astimezone(org_tz).isoformat()}" if t.deadline else ""
                if t.proposed_deadline:
                    proposer = name_map.get(t.proposed_deadline_by, str(t.proposed_deadline_by)) if t.proposed_deadline_by else "assignee"
                    dl += f", proposed_deadline: {t.proposed_deadline.astimezone(org_tz).isoformat()} (by {proposer})"
                assignee = name_map.get(t.assignee_user_id, str(t.assignee_user_id))
                creator = name_map.get(t.created_by_user_id, str(t.created_by_user_id)) if t.created_by_user_id else ""
                participant_names = [
                    p["name"] for p in participants_by_task.get(t.id, [])
                ]
                participants = (
                    f", participants={', '.join(participant_names)}"
                    if participant_names else ""
                )
                desc = f", description={t.description!r}" if (t.description and include_descriptions) else ""
                lines.append(
                    f"- [{t.status}] {t.title}{dl}"
                    f" (id={t.id}, priority={t.priority}, assignee={assignee}{f', creator={creator}' if creator else ''}{participants}{desc})"
                )

            span = f"{start + 1}–{end} of {total}"
            header = f"Tasks {span} (page {page}/{total_pages}):"
            footer = (
                "\nTASK DESCRIPTIONS HIDDEN TO PREVENT OUTPUT FLOOD."
                " SHRINK OUTPUT USING FILTERS TO CHECK DESCRIPTIONS IF YOU NEED"
                if not include_descriptions else ""
            )
            logger.info(
                "task_query done: total=%d shown=%d include_desc=%s",
                total,
                len(shown),
                include_descriptions,
            )
            return header + "\n" + "\n".join(lines) + footer
        except Exception as e:
            logger.error(f"task_query failed: {e}", exc_info=True)
            if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
                return f"Invalid UUID in input: {e}"
            return _TOOL_ERROR_RESULT

    async def _task_update_async(
        task_id: str,
        status: Literal["done", "created", "in_progress", "awaiting_review"] = "",
        title: Optional[str] = "",
        description: Optional[str] = "",
        deadline: Optional[str] = None,
        priority: Optional[int] = None,
        new_participant_user_ids: Optional[List[str]] = None,
        delete_participant_user_ids: Optional[List[str]] = None,
        config: RunnableConfig = None,
    ) -> str:
        """Modify an existing task. Use this when changing an already-created task — do NOT call task_create for edits.
        Args:
            task_id: UUID of the task (get it from task_query output)
            status: New status (created, in_progress, done)
            title: New title (empty = keep current)
            description: New description (empty = keep current)
            deadline: New deadline in ISO format (only creator or admin can set)
            priority: New priority: 1 (Обычно), 2 (Важно), 3 (Срочно). Only the task creator can set this.
            new_participant_user_ids: Optional UUIDs of task participants to add.
            delete_participant_user_ids: Optional UUIDs of task participants to remove.
        """
        logger.info(
            f"tool task_update: task={task_id} status={status!r} title_set={bool(title)} "
            f"deadline={deadline!r} priority={priority} "
            f"add_participants={new_participant_user_ids} remove_participants={delete_participant_user_ids}"
        )
        try:
            configurable = (config or {}).get("configurable", {})
            _log_identity("task_update", configurable)
            caller_uuid = UUID(configurable["caller_user_id"])
            is_admin = configurable.get("is_admin", False)
            org_tz = configurable.get("timezone", "Europe/Moscow")

            task_uuid = UUID(task_id)
            add_participant_uuids = _parse_uuid_list(new_participant_user_ids)
            remove_participant_uuids = _parse_uuid_list(delete_participant_user_ids)
            updated = None
            participant_changes = []
            try:
                deadline_dt = _parse_deadline(deadline, org_tz) if deadline else None
            except ValueError:
                logger.info("task_update invalid_deadline: task=%s deadline=%r", task_id, deadline)
                return f"Invalid deadline format: {deadline!r}. Use ISO format, e.g. '2025-03-15T18:00:00'."
            if priority is not None and priority not in (1, 2, 3):
                logger.info("task_update invalid_priority: task=%s priority=%s", task_uuid, priority)
                return "priority must be 1 (Обычно), 2 (Важно) or 3 (Срочно)"
            has_status_update = bool(status)
            has_field_update = bool(
                title
                or description
                or add_participant_uuids
                or remove_participant_uuids
            )

            existing_task = await task_service.get(task_uuid)
            if not existing_task:
                logger.info("task_update not_found: task=%s", task_id)
                return f"Task {task_id} not found"
            is_creator = existing_task.created_by_user_id == caller_uuid
            is_assignee = existing_task.assignee_user_id == caller_uuid
            logger.info(
                "task_update permission: task=%s current=%s requested=%s is_creator=%s is_assignee=%s is_admin=%s has_fields=%s deadline=%s priority=%s add_participants=%d remove_participants=%d",
                task_uuid, existing_task.status, status, is_creator, is_assignee, is_admin,
                has_field_update, bool(deadline_dt), priority, len(add_participant_uuids), len(remove_participant_uuids),
            )

            _ALLOWED_STATUSES = ("created", "in_progress", "awaiting_review", "done")
            if has_status_update and status not in _ALLOWED_STATUSES:
                logger.info("task_update invalid_status: task=%s requested=%s", task_uuid, status)
                return (
                    f"'{status}' is not a valid status. "
                    f"Allowed values: {', '.join(_ALLOWED_STATUSES)}."
                )

            if has_status_update:
                if err := _check_status_transition(existing_task.status, status, is_creator, is_assignee):
                    logger.info("task_update denied: task=%s reason=status_transition current=%s requested=%s",
                                task_uuid, existing_task.status, status)
                    return err

            is_self_assigned = is_creator and is_assignee
            if deadline_dt and existing_task.status == "overdue" and not is_self_assigned:
                logger.info("task_update denied: task=%s reason=overdue_deadline", task_uuid)
                return "Cannot change the deadline of an overdue task."

            if deadline_dt and not is_creator and not is_admin and not is_assignee:
                logger.info("task_update denied: task=%s reason=deadline_permission", task_uuid)
                return "Only the task creator, admin, or assignee can change the deadline."

            if priority is not None and not is_creator:
                logger.info("task_update denied: task=%s reason=priority_permission", task_uuid)
                return "Only the task creator can change the priority."

            if has_field_update and not is_admin and not is_creator:
                logger.info("task_update denied: task=%s reason=field_permission", task_uuid)
                return "Only the task creator or an admin can update task fields (title, description, participants)."

            # Apply field updates before status so the final state reflects both changes.
            if title or description or priority is not None:
                updated = await task_service.update(
                    task_id=task_uuid,
                    title=title or None,
                    description=description or None,
                    priority=priority or None,
                    actor_user_id=caller_uuid,
                )
                if not updated:
                    return f"Task {task_id} not found"
                logger.info("task_update applied: task=%s operation=fields title_set=%s description_set=%s priority=%s",
                            task_uuid, bool(title), bool(description), priority)

            if deadline_dt:
                from ...services.engine_service import get_engine_service
                engine = get_engine_service()
                caller_user = await engine.user_storage.get_by_id(caller_uuid)
                if caller_user is None:
                    logger.info("task_update caller_not_found: task=%s user=%s", task_uuid, caller_uuid)
                    return "CURRENT USER NOT FOUND"
                if is_creator or is_admin:
                    # Creator/admin sets the deadline directly — takes effect immediately.
                    updated = await task_service.set_deadline(task_uuid, caller_user, deadline_dt)
                    logger.info("task_update applied: task=%s operation=set_deadline deadline=%s",
                                task_uuid, deadline_dt.isoformat())
                else:
                    # Assignee can only propose a new deadline; creator must accept it.
                    updated = await task_service.propose_deadline(task_uuid, caller_user, deadline_dt)
                    logger.info("task_update done: task=%s result=deadline_proposed deadline=%s",
                                task_uuid, deadline_dt.isoformat())
                    return (
                        f"Deadline proposal submitted: {deadline_dt.isoformat()}. "
                        "The task creator must accept it for it to take effect."
                    )
            if status:
                updated = await task_service.update_status(task_uuid, status)
                if not updated:
                    return f"Task {task_id} not found"
                logger.info("task_update applied: task=%s operation=status status=%s", task_uuid, status)

            if add_participant_uuids or remove_participant_uuids:
                from ...services.engine_service import get_engine_service
                engine = get_engine_service()
                initiator = await engine.user_storage.get_by_id(caller_uuid)
                if initiator is None:
                    logger.info("task_update initiator_not_found: task=%s user=%s", task_uuid, caller_uuid)
                    return "CURRENT USER NOT FOUND"

                for participant_uuid in add_participant_uuids:
                    await task_service.add_participant(
                        task_uuid, participant_uuid, initiator,
                    )
                    participant_changes.append(f"added participant {participant_uuid}")
                    logger.info("task_update applied: task=%s operation=add_participant participant=%s",
                                task_uuid, participant_uuid)

                for participant_uuid in remove_participant_uuids:
                    removed = await task_service.remove_participant(
                        task_uuid, participant_uuid, initiator,
                    )
                    if removed:
                        participant_changes.append(f"removed participant {participant_uuid}")
                    else:
                        participant_changes.append(f"participant {participant_uuid} was not present")
                    logger.info("task_update applied: task=%s operation=remove_participant participant=%s removed=%s",
                                task_uuid, participant_uuid, removed)

            if updated is None and participant_changes:
                updated = await task_service.get(task_uuid)
                if not updated:
                    return f"Task {task_id} not found"

            if updated is None:
                logger.info("task_update done: task=%s result=noop", task_uuid)
                return (
                    "Nothing to update: no status, title, description, "
                    "priority, participants to add, or participants to remove provided"
                )

            participant_suffix = (
                f", participants: {'; '.join(participant_changes)}"
                if participant_changes else ""
            )
            logger.info("task_update done: task=%s status=%s participant_changes=%d",
                        task_uuid, updated.status, len(participant_changes))
            return (
                f"Task '{updated.title}' updated "
                f"(status={updated.status}, priority={updated.priority}, description={updated.description}{participant_suffix})"
            )
        except Exception as e:
            logger.error(f"task_update failed: {e}", exc_info=True)
            if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
                return f"Invalid UUID in input: {e}"
            return _TOOL_ERROR_RESULT

    async def _task_deadline_proposal_async(
        task_id: str,
        accept: bool,
        config: RunnableConfig = None,
    ) -> str:
        """Accept or reject an assignee's proposed deadline. Only the task creator can call this."""
        logger.info("tool task_deadline_proposal: task=%s accept=%s", task_id, accept)
        try:
            configurable = (config or {}).get("configurable", {})
            _log_identity("task_deadline_proposal", configurable)
            caller_uuid = UUID(configurable["caller_user_id"])

            from ...services.engine_service import get_engine_service
            engine = get_engine_service()

            caller_user = await engine.user_storage.get_by_id(caller_uuid)
            if caller_user is None:
                logger.info("task_deadline_proposal caller_not_found: task=%s user=%s", task_id, caller_uuid)
                return "CURRENT USER NOT FOUND"

            task_uuid = UUID(task_id)
            task = await task_service.get(task_uuid)
            if not task:
                logger.info("task_deadline_proposal not_found: task=%s", task_uuid)
                return f"Task {task_id} not found."
            if task.proposed_deadline is None:
                logger.info("task_deadline_proposal no_pending: task=%s", task_uuid)
                return "This task has no pending deadline proposal."
            if task.created_by_user_id != caller_user.id:
                logger.info(
                    "task_deadline_proposal denied: task=%s caller=%s creator=%s",
                    task_uuid,
                    caller_user.id,
                    task.created_by_user_id,
                )
                return "Only the task creator can accept or reject a deadline proposal."

            if accept:
                updated = await task_service.accept_proposed_deadline(task_uuid, caller_user)
                logger.info(
                    "task_deadline_proposal done: task=%s result=accepted deadline=%s",
                    task_uuid,
                    updated.deadline.isoformat() if updated.deadline else "",
                )
                return f"Deadline proposal accepted. New deadline: {updated.deadline.isoformat()}."
            else:
                await task_service.reject_proposed_deadline(task_uuid, caller_user)
                logger.info("task_deadline_proposal done: task=%s result=rejected", task_uuid)
                return "Deadline proposal rejected."
        except Exception as e:
            logger.error("task_deadline_proposal failed: %s", e, exc_info=True)
            if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
                return f"Invalid UUID in input: {e}"
            return _TOOL_ERROR_RESULT

    create_tool = StructuredTool.from_function(
        coroutine=_task_create_async,
        name="task_create",
        description="Create a task for an employee. Use when a manager assigns work via chat (e.g. '@@oleg check the contract by Friday').",
        args_schema=TaskCreateInput,
    )

    query_tool = StructuredTool.from_function(
        coroutine=_task_query_async,
        name="task_query",
        description="Query tasks. Filter by employee and/or status (created, in_progress, done, overdue).",
        args_schema=TaskQueryInput,
    )

    update_tool = StructuredTool.from_function(
        coroutine=_task_update_async,
        name="task_update",
        description=(
            "Modify an existing task: change status, title, or description. "
            "Use this for ANY change to an already-created task — do NOT call task_create "
            "to 'update' an existing task. Requires the task UUID from task_query."
        ),
        args_schema=TaskUpdateInput,
    )

    deadline_proposal_tool = StructuredTool.from_function(
        coroutine=_task_deadline_proposal_async,
        name="task_deadline_proposal",
        description="Accept or reject an assignee's proposed deadline on a task. Only the task creator can call this.",
        args_schema=TaskDeadlineProposalInput,
    )

    async def _get_own_tasks_async(
        status: Optional[Literal["done", "created", "in_progress"]] = None,
        page: int = 1,
        config: RunnableConfig = None,
    ) -> str:
        """Return tasks assigned to the role owner.
        In a mention invocation (@@role) returns the callee's tasks; otherwise the caller's.
        Args:
            status: Optional status filter. Empty = all active tasks excluding done and overdue older than 30 days.
            page: Page number (page size 50).
        """
        logger.info("tool get_own_tasks: status=%r page=%d", status, page)
        try:
            configurable = (config or {}).get("configurable", {})
            _log_identity("get_own_tasks", configurable)
            # callee_user_id == caller_user_id in direct calls (always set by executor).
            target_uuid = UUID(configurable.get("callee_user_id") or configurable["caller_user_id"])
            org_tz = zoneinfo.ZoneInfo(configurable.get("timezone", "Europe/Moscow"))

            tasks = await task_service.list_by_assignee(target_uuid, status or None)
            initial_count = len(tasks)
            excluded_done = 0
            excluded_old_overdue = 0
            excluded_overdue_no_deadline = 0

            # Default view: hide done tasks and overdue tasks whose deadline passed >30 days ago.
            if status is None:
                cutoff = datetime.now().replace(tzinfo=None)
                filtered = []
                for t in tasks:
                    if t.status == "done":
                        excluded_done += 1
                        continue
                    if t.status == "overdue":
                        if t.deadline is None:
                            excluded_overdue_no_deadline += 1
                            continue
                        dl = t.deadline.replace(tzinfo=None) if hasattr(t.deadline, "tzinfo") else t.deadline
                        if (cutoff - dl).days > 30:
                            excluded_old_overdue += 1
                            continue
                    filtered.append(t)
                tasks = filtered
            logger.info(
                "get_own_tasks filter: target=%s status=%s initial=%d after=%d excluded_done=%d excluded_old_overdue=%d excluded_overdue_no_deadline=%d",
                target_uuid,
                status,
                initial_count,
                len(tasks),
                excluded_done,
                excluded_old_overdue,
                excluded_overdue_no_deadline,
            )

            if not tasks:
                logger.info("get_own_tasks done: target=%s result=empty", target_uuid)
                return "No tasks found."

            total = len(tasks)
            page = max(1, page)
            total_pages = max(1, (total + _TASKS_PAGE_SIZE - 1) // _TASKS_PAGE_SIZE)
            page = min(page, total_pages)
            start = (page - 1) * _TASKS_PAGE_SIZE
            end = min(start + _TASKS_PAGE_SIZE, total)
            shown = tasks[start:end]
            logger.info(
                "get_own_tasks page: target=%s page=%d/%d span=%d-%d total=%d shown=%d",
                target_uuid,
                page,
                total_pages,
                start + 1,
                end,
                total,
                len(shown),
            )

            from ...services.engine_service import get_engine_service
            engine = get_engine_service()
            user_ids = {t.created_by_user_id for t in shown if t.created_by_user_id}
            users = await engine.user_storage.get_certain_users(list(user_ids))
            name_map = {u.id: u.name for u in users}
            participants_by_task = await engine.task_participant_storage.get_for_tasks(
                [t.id for t in shown],
            )

            lines = []
            for t in shown:
                dl = f", deadline: {t.deadline.astimezone(org_tz).isoformat()}" if t.deadline else ""
                if t.proposed_deadline:
                    proposer = name_map.get(t.proposed_deadline_by, str(t.proposed_deadline_by)) if t.proposed_deadline_by else "assignee"
                    dl += f", proposed_deadline: {t.proposed_deadline.astimezone(org_tz).isoformat()} (by {proposer})"
                creator = name_map.get(t.created_by_user_id, str(t.created_by_user_id)) if t.created_by_user_id else ""
                participant_names = [p["name"] for p in participants_by_task.get(t.id, [])]
                participants = f", participants={', '.join(participant_names)}" if participant_names else ""
                desc = f", description={t.description!r}" if t.description else ""
                lines.append(
                    f"- [{t.status}] {t.title}{dl}"
                    f" (creator={creator}{participants}{desc})"
                )

            span = f"{start + 1}–{end} of {total}"
            logger.info("get_own_tasks done: target=%s total=%d shown=%d", target_uuid, total, len(shown))
            return f"Tasks {span} (page {page}/{total_pages}):\n" + "\n".join(lines)
        except Exception as e:
            logger.error("get_own_tasks failed: %s", e, exc_info=True)
            if isinstance(e, ValueError) and "badly formed hexadecimal UUID string" in str(e):
                return f"Invalid UUID in input: {e}"
            return _TOOL_ERROR_RESULT

    get_own_tasks_tool = StructuredTool.from_function(
        coroutine=_get_own_tasks_async,
        name="get_own_tasks",
        description=(
            "Get tasks assigned to the role owner. "
            "In a mention call (@@role) returns the callee's tasks; in direct chat returns the caller's. "
            "By default excludes done tasks and overdue tasks whose deadline passed more than 30 days ago. "
            "Optionally filter by status: created, in_progress, done. Supports pagination via page."
        ),
        args_schema=GetOwnTasksInput,
    )

    return create_tool, query_tool, update_tool, deadline_proposal_tool, get_own_tasks_tool
