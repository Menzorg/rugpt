"""
Task Tools

LangChain tools for task management (create, query, update).
Uses factory function to inject TaskService dependency.

Tools are async — invoked directly in the running event loop (the same one
that owns the asyncpg pool), so we just `await` service calls. No threading,
no nested asyncio.run(). This avoids the `There is no current event loop`
errors that the sync-wrapper approach produced under langchain-openai.
"""
import logging
from datetime import date, datetime
from typing import Annotated, Literal, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

from ...config import Config
from ...services.task_service import TaskService

logger = logging.getLogger("rugpt.agents.tools.task")


# ============================================
# Tool input schemas
# ============================================

class TaskCreateInput(BaseModel):
    title: str = Field(description="Task title")
    description: str = Field(default="", description="Task description")
    assignee_user_id: str = Field(description="UUID of the employee to assign the task to")
    deadline: str = Field(default="", description="Deadline in ISO format (e.g. 2025-03-15T18:00:00)")


class TaskQueryInput(BaseModel):
    assignee_user_id: str = Field(default="", description="Filter by UUID of employee the task is assigned to (empty = any)")
    created_by_user_id: str = Field(default="", description="Filter by UUID of the user who CREATED the task (empty = any)")
    status: Literal["done", "created", "in_progress"] | None = Field(default=None, description="Filter by status: created, in_progress, done, overdue (empty = all)")
    text_search_query: str = Field(default="", description="Full-text search over task title and description (empty = skip)")
    deadline_from: Optional[date] = Field(default=None, description="Filter tasks with deadline on or after this date, YYYY-MM-DD (empty = no lower bound)")
    deadline_to: Optional[date] = Field(default=None, description="Filter tasks with deadline on or before this date, YYYY-MM-DD (empty = no upper bound)")
    created_from: Optional[date] = Field(default=None, description="Filter tasks created on or after this date, YYYY-MM-DD (empty = no lower bound)")
    created_to: Optional[date] = Field(default=None, description="Filter tasks created on or before this date, YYYY-MM-DD (empty = no upper bound)")


class TaskUpdateInput(BaseModel):
    task_id: str = Field(description="UUID of the task to update")
    status: str = Field(default="", description="New status: created, in_progress, done")
    title: str = Field(default="", description="New title (empty = keep current)")
    description: str = Field(default="", description="New description (empty = keep current)")


# ============================================
# Factory: create tools wired to TaskService
# ============================================

def create_task_tools(
    task_service: TaskService,
):
    """
    Create task tools wired to a real TaskService instance.

    Returns (task_create_tool, task_query_tool, task_update_tool).
    """

    async def _task_create_async(
        title: str,
        assignee_user_id: str,
        deadline: Optional[str] = "",
        description: Optional[str] = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Create a task for an employee. Use when a manager assigns work via chat.
        Args:
            title: Task title
            assignee_user_id: UUID of the employee
            description: Task description
            deadline: Deadline in ISO format
        """
        logger.info(
            f"tool task_create: title={title!r} assignee={assignee_user_id} deadline={deadline!r}"
        )
        try:
            configurable = (config or {}).get("configurable", {})
            user_id = configurable.get("user_id", "")
            org_id = configurable.get("org_id", "")

            assignee_uuid = UUID(assignee_user_id)
            dl = datetime.fromisoformat(deadline) if deadline else None

            # org_id is injected by the executor from the caller's context; absent means
            # the tool was invoked outside a proper agent run — refuse rather than guess.
            task_org_id = UUID(org_id) if org_id else None
            if task_org_id is None:
                return "System can't see user's organization id"

            # Refuse if the caller's department visibility rules don't include the assignee.
            if user_id and org_id:
                from ...services.engine_service import get_engine_service
                engine = get_engine_service()
                visible = await engine.department_service.check_visible(
                    UUID(user_id), assignee_uuid, UUID(org_id),
                )
                if not visible:
                    return "Cannot assign task: user not visible to you."

            task = await task_service.create(
                org_id=task_org_id,
                title=title,
                description=description or None,
                assignee_user_id=assignee_uuid,
                deadline=dl,
                created_by_user_id=UUID(user_id) if user_id else None,
            )
            return f"Task '{title}' created (id={task.id})"
        except Exception as e:
            logger.error(f"task_create failed: {e}")
            return f"Failed to create task: {e}"

    async def _task_query_async(
        assignee_user_id: Optional[str] = "",
        created_by_user_id: Optional[str] = "",
        status: Optional[Literal["done", "created", "in_progress"]] = "",
        text_search_query: Optional[str] = "",
        deadline_from: Optional[date] = None,
        deadline_to: Optional[date] = None,
        created_from: Optional[date] = None,
        created_to: Optional[date] = None,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
            user_id = configurable.get("user_id", "")
            org_id = configurable.get("org_id", "")

            query_org_id = UUID(org_id) if org_id else None
            if query_org_id is None:
                return "System can't see user's organization id"

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

            if created_by_user_id:
                rows = await task_service.list_tasks_created_by(UUID(created_by_user_id))
                creator_tasks = [r["task"] for r in rows]
                filter_sets.append({t.id for t in creator_tasks})
                _add_to_pool(creator_tasks)

            if status and not assignee_user_id:
                # status is already passed into list_by_assignee above; only
                # fetch by status separately when no assignee filter was given.
                status_tasks = await task_service.list_by_org(query_org_id, status)
                filter_sets.append({t.id for t in status_tasks})
                _add_to_pool(status_tasks)

            if text_search_query:
                search_tasks = await task_service.text_search(query_org_id, text_search_query)
                filter_sets.append({t.id for t in search_tasks})
                _add_to_pool(search_tasks)

            if deadline_from or deadline_to:
                deadline_tasks = await task_service.list_by_deadline_range(
                    query_org_id, deadline_from=deadline_from, deadline_to=deadline_to,
                )
                filter_sets.append({t.id for t in deadline_tasks})
                _add_to_pool(deadline_tasks)

            if created_from or created_to:
                created_tasks = await task_service.list_by_created_range(
                    query_org_id, created_from=created_from, created_to=created_to,
                )
                filter_sets.append({t.id for t in created_tasks})
                _add_to_pool(created_tasks)

            # No filter at all → return all org tasks
            if not filter_sets:
                all_tasks = await task_service.list_by_org(query_org_id, status or None)
                _add_to_pool(all_tasks)
                final_ids = {t.id for t in pool}
            else:
                final_ids = set.intersection(*filter_sets)

            tasks = [t for t in pool if t.id in final_ids]

            # Strip tasks whose assignees are outside the caller's department visibility.
            if user_id and org_id:
                visible_ids = await engine.department_service.get_visible_user_ids(
                    UUID(user_id), UUID(org_id),
                )
                tasks = [t for t in tasks if t.assignee_user_id in visible_ids]

            if not tasks:
                return "No tasks found."

            total = len(tasks)
            limited = total > Config.TASKS_QUERY_LIMIT
            shown = tasks[:Config.TASKS_QUERY_LIMIT]

            # TODO: replace char budget with token budget via a token counting service
            total_desc_chars = sum(len(t.description) for t in shown if t.description)
            include_descriptions = total_desc_chars <= Config.TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET

            # Resolve all referenced user IDs to names in one batch query.
            from ...services.engine_service import get_engine_service
            engine = get_engine_service()
            user_ids = {t.assignee_user_id for t in shown if t.assignee_user_id}
            user_ids |= {t.created_by_user_id for t in shown if t.created_by_user_id}
            users = await engine.user_storage.get_certain_users(list(user_ids))
            name_map = {u.id: u.name for u in users}

            lines = []
            for t in shown:
                dl = f", deadline: {t.deadline.isoformat()}" if t.deadline else ""
                assignee = name_map.get(t.assignee_user_id, str(t.assignee_user_id))
                creator = name_map.get(t.created_by_user_id, str(t.created_by_user_id)) if t.created_by_user_id else ""
                desc = f", description={t.description!r}" if (t.description and include_descriptions) else ""
                lines.append(
                    f"- [{t.status}] {t.title}{dl}"
                    f" (id={t.id}, assignee={assignee}{f', creator={creator}' if creator else ''}{desc})"
                )

            header = f"Tasks ({total} total{f', LIMITED TO {Config.TASKS_QUERY_LIMIT}' if limited else ''}):"
            footer = (
                "\nTASK DESCRIPTIONS HIDDEN TO PREVENT OUTPUT FLOOD."
                " SHRINK OUTPUT USING FILTERS TO CHECK DESCRIPTIONS IF YOU NEED"
                if not include_descriptions else ""
            )
            return header + "\n" + "\n".join(lines) + footer
        except Exception as e:
            logger.error(f"task_query failed: {e}")
            return f"Failed to query tasks: {e}"

    async def _task_update_async(
        task_id: str,
        status: Literal["done", "created", "in_progress"] = "",
        title: Optional[str] = "",
        description: Optional[str] = "",
    ) -> str:
        """Modify an existing task. Use this when changing an already-created task — do NOT call task_create for edits.
        Args:
            task_id: UUID of the task (get it from task_query output)
            status: New status (created, in_progress, done)
            title: New title (empty = keep current)
            description: New description (empty = keep current)
        """
        logger.info(
            f"tool task_update: task={task_id} status={status!r} title_set={bool(title)}"
        )
        try:
            task_uuid = UUID(task_id)
            updated = None
            # Apply field updates before status so the final state reflects both changes.
            if title or description:
                updated = await task_service.update(
                    task_id=task_uuid,
                    title=title or None,
                    description=description or None,
                )
                if not updated:
                    return f"Task {task_id} not found"
            if status:
                updated = await task_service.update_status(task_uuid, status)
                if not updated:
                    return f"Task {task_id} not found"
            if updated is None:
                return "Nothing to update: no status, title, or description provided"
            return f"Task '{updated.title}' updated (status={updated.status}, description={updated.description})"
        except ValueError as e:
            logger.error(f"task_update validation failed: {e}")
            return f"Invalid input: {e}"
        except Exception as e:
            logger.error(f"task_update failed: {e}")
            return f"Failed to update task: {e}"

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

    return create_tool, query_tool, update_tool
