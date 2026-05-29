from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.agents.tools.task_tool import create_task_tools
from src.engine.models.task import Task


@pytest.mark.asyncio
async def test_task_query_output_includes_priority(monkeypatch):
    org_id = uuid4()
    caller_id = uuid4()
    assignee_id = uuid4()
    task = Task(
        org_id=org_id,
        title="Important task",
        status="created",
        assignee_user_id=assignee_id,
        priority=3,
    )

    task_service = AsyncMock()
    task_service.list_by_org = AsyncMock(return_value=[task])

    engine = SimpleNamespace(
        department_service=SimpleNamespace(
            get_visible_user_ids=AsyncMock(return_value={assignee_id}),
        ),
        user_storage=SimpleNamespace(
            get_certain_users=AsyncMock(
                return_value=[SimpleNamespace(id=assignee_id, name="Assignee")]
            ),
        ),
        task_participant_storage=SimpleNamespace(
            get_for_tasks=AsyncMock(return_value={}),
        ),
    )
    monkeypatch.setattr(
        "src.engine.services.engine_service.get_engine_service",
        lambda: engine,
    )

    query_tool = create_task_tools(task_service)[1]
    result = await query_tool.coroutine(
        config={
            "configurable": {
                "caller_user_id": str(caller_id),
                "org_id": str(org_id),
            }
        }
    )

    assert "priority=3" in result
    assert "id=" in result
    assert "assignee=Assignee" in result
