from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

from src.engine.storage.project_storage import ProjectStorage


@pytest.mark.asyncio
async def test_list_visible_for_user_sql_no_department():
    storage = ProjectStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    await storage.list_visible_for_user(uuid4(), uuid4(), include_archived=False)
    sql = storage.fetch.call_args.args[0]
    assert "p.created_by_user_id = $1" in sql
    assert "task_participants tp" in sql
    assert "t.assignee_user_id = $1" in sql
    assert "$4::uuid IS NOT NULL AND p.department_id = $4" in sql


@pytest.mark.asyncio
async def test_list_visible_for_user_passes_department():
    storage = ProjectStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    uid, org, dep = uuid4(), uuid4(), uuid4()
    await storage.list_visible_for_user(uid, org, include_archived=True, department_id=dep)
    args = storage.fetch.call_args.args
    assert args[1] == uid
    assert args[2] == org
    assert args[3] is True
    assert args[4] == dep
