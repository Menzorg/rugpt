"""
Tests for ReferenceService (item 11): parsing and batch resolution of
!<task_uuid> and !!<project_uuid>.
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from src.engine.models.project import Project
from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.reference_service import ReferenceService, REFERENCE_PATTERN


def make_user(is_admin=False, org_id=None, uid=None):
    return User(
        id=uid or uuid4(), org_id=org_id or uuid4(),
        name="u", username="u", email="u@u",
        is_admin=is_admin,
    )


def make_service():
    return ReferenceService(task_storage=AsyncMock(), project_storage=AsyncMock())


# === parse() ===

def test_parse_task_reference():
    svc = make_service()
    tid = uuid4()
    refs = svc.parse(f"hello !{tid} world")
    assert len(refs) == 1
    assert refs[0][0] == "task"
    assert refs[0][1] == tid


def test_parse_project_reference():
    svc = make_service()
    pid = uuid4()
    refs = svc.parse(f"see !!{pid} now")
    assert len(refs) == 1
    assert refs[0][0] == "project"
    assert refs[0][1] == pid


def test_parse_double_before_single():
    """`!!abc...` must not be chewed as single `!`."""
    svc = make_service()
    pid = uuid4()
    refs = svc.parse(f"!!{pid}")
    assert len(refs) == 1
    assert refs[0][0] == "project"


def test_parse_mixed_order_preserved():
    svc = make_service()
    tid = uuid4()
    pid = uuid4()
    refs = svc.parse(f"!{tid} then !!{pid}")
    assert [r[0] for r in refs] == ["task", "project"]
    assert refs[0][2] < refs[1][2]  # positions ordered


def test_parse_invalid_uuid_silently_skipped():
    svc = make_service()
    # 36 dashes - right length, wrong shape
    junk = "-" * 36
    refs = svc.parse(f"!{junk}")
    assert refs == []


def test_parse_empty_content():
    svc = make_service()
    assert svc.parse("") == []
    assert svc.parse(None) == []


# === resolve_batch ===

def _task(tid, org_id, creator, assignee, title="T"):
    return Task(
        id=tid, org_id=org_id,
        title=title, assignee_user_id=assignee,
        created_by_user_id=creator,
    )


def test_resolve_batch_task_creator_visible():
    async def go():
        svc = make_service()
        org = uuid4()
        creator = make_user(org_id=org)
        task = _task(uuid4(), org, creator.id, uuid4())
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={task.id: task})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!{task.id}")], creator)
        assert out[msg_id][0]["accessible"] is True
        assert out[msg_id][0]["title"] == "T"
    asyncio.run(go())


def test_resolve_batch_task_not_visible():
    async def go():
        svc = make_service()
        org = uuid4()
        task = _task(uuid4(), org, uuid4(), uuid4(), title="Secret")
        stranger = make_user(org_id=org)  # same org but not creator/assignee/admin
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={task.id: task})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!{task.id}")], stranger)
        assert out[msg_id][0]["accessible"] is False
        assert out[msg_id][0]["title"] is None
    asyncio.run(go())


def test_resolve_batch_task_cross_org_not_accessible():
    async def go():
        svc = make_service()
        task = _task(uuid4(), uuid4(), uuid4(), uuid4(), title="Foreign")
        outsider = make_user()  # different org
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={task.id: task})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!{task.id}")], outsider)
        assert out[msg_id][0]["accessible"] is False
    asyncio.run(go())


def test_resolve_batch_project_same_org_accessible():
    async def go():
        svc = make_service()
        org = uuid4()
        user = make_user(org_id=org)
        project = Project(org_id=org, name="P", is_active=True)
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={project.id: project})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!!{project.id}")], user)
        assert out[msg_id][0]["accessible"] is True
        assert out[msg_id][0]["title"] == "P"
    asyncio.run(go())


def test_resolve_batch_project_archived_not_accessible():
    async def go():
        svc = make_service()
        org = uuid4()
        user = make_user(org_id=org)
        project = Project(org_id=org, name="P", is_active=False)
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={project.id: project})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!!{project.id}")], user)
        assert out[msg_id][0]["accessible"] is False
    asyncio.run(go())


def test_resolve_batch_project_cross_org_not_accessible():
    async def go():
        svc = make_service()
        user = make_user()
        project = Project(org_id=uuid4(), name="Foreign", is_active=True)
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={project.id: project})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!!{project.id}")], user)
        assert out[msg_id][0]["accessible"] is False
    asyncio.run(go())


def test_resolve_batch_mixed_entities():
    async def go():
        svc = make_service()
        org = uuid4()
        user = make_user(org_id=org, is_admin=True)
        task = _task(uuid4(), org, user.id, uuid4(), title="T1")
        project = Project(org_id=org, name="Pr", is_active=True)
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={task.id: task})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={project.id: project})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"a !{task.id} b !!{project.id}")], user)
        assert len(out[msg_id]) == 2
        types = [r["type"] for r in out[msg_id]]
        assert types == ["task", "project"]
    asyncio.run(go())


def test_resolve_batch_deleted_entity():
    async def go():
        svc = make_service()
        user = make_user()
        missing_id = uuid4()
        svc.task_storage.get_many_by_ids = AsyncMock(return_value={})
        svc.project_storage.get_many_by_ids = AsyncMock(return_value={})
        msg_id = uuid4()
        out = await svc.resolve_batch([(msg_id, f"!{missing_id}")], user)
        assert out[msg_id][0]["accessible"] is False
        assert out[msg_id][0]["title"] is None
    asyncio.run(go())
