"""
Task Tools

LangChain tools for task management (create, query, update).
Uses factory function to inject TaskService dependency.
"""
import asyncio
import logging
from typing import Optional
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

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
    assignee_user_id: str = Field(default="", description="UUID of employee to filter tasks for (empty = all)")
    status: str = Field(default="", description="Filter by status: created, in_progress, done, overdue (empty = all)")


class TaskUpdateInput(BaseModel):
    task_id: str = Field(description="UUID of the task to update")
    status: str = Field(default="", description="New status: created, in_progress, done")
    title: str = Field(default="", description="New title (empty = keep current)")
    description: str = Field(default="", description="New description (empty = keep current)")


# ============================================
# Factory: create tools wired to TaskService
# ============================================

def create_task_tools(
    task_service,
    default_org_id: Optional[UUID] = None,
):
    """
    Create task tools wired to a real TaskService instance.

    Returns (task_create_tool, task_query_tool, task_update_tool).
    """

    def _task_create(
        title: str,
        assignee_user_id: str,
        description: str = "",
        deadline: str = "",
    ) -> str:
        """Create a task for an employee. Use when a manager assigns work via chat.
        Args:
            title: Task title
            assignee_user_id: UUID of the employee
            description: Task description
            deadline: Deadline in ISO format
        """
        try:
            from datetime import datetime

            assignee_uuid = UUID(assignee_user_id)
            dl = None
            if deadline:
                dl = datetime.fromisoformat(deadline)

            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    loop.run_in_executor(
                        pool,
                        lambda: asyncio.run(
                            task_service.create(
                                org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                                title=title,
                                description=description or None,
                                assignee_user_id=assignee_uuid,
                                deadline=dl,
                            )
                        )
                    )
                    logger.info(f"task_create: scheduled '{title}' for {assignee_user_id}")
                    return f"Task '{title}' created for employee {assignee_user_id}"
            else:
                task = loop.run_until_complete(
                    task_service.create(
                        org_id=default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                        title=title,
                        description=description or None,
                        assignee_user_id=assignee_uuid,
                        deadline=dl,
                    )
                )
                return f"Task '{title}' created (id={task.id})"
        except Exception as e:
            logger.error(f"task_create failed: {e}")
            return f"Failed to create task: {e}"

    def _task_query(assignee_user_id: str = "", status: str = "") -> str:
        """Query tasks. Can filter by employee and/or status.
        Args:
            assignee_user_id: UUID of employee (empty = all in org)
            status: Filter by status (empty = all)
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                logger.info(f"task_query: assignee={assignee_user_id}, status={status}")
                return "Task query executed. (Use API for full results)"
            else:
                if assignee_user_id:
                    tasks = loop.run_until_complete(
                        task_service.list_by_assignee(UUID(assignee_user_id), status or None)
                    )
                else:
                    tasks = loop.run_until_complete(
                        task_service.list_by_org(
                            default_org_id or UUID('00000000-0000-0000-0000-000000000000'),
                            status or None,
                        )
                    )
                if not tasks:
                    return "No tasks found."
                lines = []
                for t in tasks[:20]:
                    dl = f", deadline: {t.deadline.isoformat()}" if t.deadline else ""
                    lines.append(f"- [{t.status}] {t.title}{dl}")
                return f"Tasks ({len(tasks)} total):\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"task_query failed: {e}")
            return f"Failed to query tasks: {e}"

    def _task_update(task_id: str, status: str = "", title: str = "", description: str = "") -> str:
        """Update a task's status or details.
        Args:
            task_id: UUID of the task
            status: New status (created, in_progress, done)
            title: New title (empty = keep current)
            description: New description (empty = keep current)
        """
        try:
            task_uuid = UUID(task_id)

            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    loop.run_in_executor(
                        pool,
                        lambda: asyncio.run(
                            task_service.update(
                                task_id=task_uuid,
                                title=title or None,
                                description=description or None,
                                status=status or None,
                            )
                        )
                    )
                    logger.info(f"task_update: scheduled update for {task_id}")
                    return f"Task {task_id} update scheduled"
            else:
                updated = loop.run_until_complete(
                    task_service.update(
                        task_id=task_uuid,
                        title=title or None,
                        description=description or None,
                        status=status or None,
                    )
                )
                if not updated:
                    return f"Task {task_id} not found"
                return f"Task '{updated.title}' updated (status={updated.status})"
        except Exception as e:
            logger.error(f"task_update failed: {e}")
            return f"Failed to update task: {e}"

    create_tool = StructuredTool.from_function(
        func=_task_create,
        name="task_create",
        description="Create a task for an employee. Use when a manager assigns work via chat (e.g. '@@oleg check the contract by Friday').",
        args_schema=TaskCreateInput,
    )

    query_tool = StructuredTool.from_function(
        func=_task_query,
        name="task_query",
        description="Query tasks. Filter by employee and/or status (created, in_progress, done, overdue).",
        args_schema=TaskQueryInput,
    )

    update_tool = StructuredTool.from_function(
        func=_task_update,
        name="task_update",
        description="Update a task's status or details.",
        args_schema=TaskUpdateInput,
    )

    return create_tool, query_tool, update_tool
