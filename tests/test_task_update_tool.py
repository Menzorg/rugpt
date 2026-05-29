from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.agents.tools.task_tool import create_task_tools
from src.engine.models.task import Task


@pytest.mark.asyncio
async def test_task_update_creator_can_change_priority():
    creator_id = uuid4()
    assignee_id = uuid4()
    task = Task(
        title="Task",
        assignee_user_id=assignee_id,
        created_by_user_id=creator_id,
        priority=1,
    )

    async def update_task(**kwargs):
        task.priority = kwargs["priority"]
        return task

    task_service = AsyncMock()
    task_service.get = AsyncMock(return_value=task)
    task_service.update = AsyncMock(side_effect=update_task)

    update_tool = create_task_tools(task_service)[2]
    result = await update_tool.coroutine(
        task_id=str(task.id),
        priority=3,
        config={
            "configurable": {
                "caller_user_id": str(creator_id),
                "is_admin": False,
            }
        },
    )

    assert "priority=3" in result
    task_service.update.assert_awaited_once()
    assert task_service.update.await_args.kwargs["priority"] == 3
    assert task_service.update.await_args.kwargs["actor_user_id"] == creator_id


@pytest.mark.asyncio
async def test_task_update_non_creator_cannot_change_priority():
    creator_id = uuid4()
    caller_id = uuid4()
    task = Task(
        title="Task",
        assignee_user_id=caller_id,
        created_by_user_id=creator_id,
        priority=1,
    )

    task_service = AsyncMock()
    task_service.get = AsyncMock(return_value=task)
    task_service.update = AsyncMock()

    update_tool = create_task_tools(task_service)[2]
    result = await update_tool.coroutine(
        task_id=str(task.id),
        priority=3,
        config={
            "configurable": {
                "caller_user_id": str(caller_id),
                "is_admin": False,
            }
        },
    )

    assert result == "Only the task creator can change the priority."
    task_service.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_update_rejects_invalid_priority():
    caller_id = uuid4()
    task_service = AsyncMock()
    task_service.get = AsyncMock()
    task_service.update = AsyncMock()

    update_tool = create_task_tools(task_service)[2]
    result = await update_tool.coroutine(
        task_id=str(uuid4()),
        priority=4,
        config={
            "configurable": {
                "caller_user_id": str(caller_id),
                "is_admin": False,
            }
        },
    )

    assert result == "priority must be 1 (Обычно), 2 (Важно) or 3 (Срочно)"
    task_service.get.assert_not_awaited()
    task_service.update.assert_not_awaited()
