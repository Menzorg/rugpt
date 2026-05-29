"""
Tests for ProjectService (item 11).

Covers:
- create requires is_head or is_admin
- multi-tenancy (get hides cross-org)
- update permissions
- soft-delete archives project chat
- duplicate names allowed
- list_by_org filters archived
"""
import asyncio
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.project import Project
from src.engine.models.user import User
from src.engine.services.project_service import ProjectService


def make_user(is_admin=False, is_head=False, org_id=None):
    return User(
        id=uuid4(),
        org_id=org_id or uuid4(),
        name="u",
        username="u",
        email="u@u",
        is_admin=is_admin,
        is_head=is_head,
    )


def make_service():
    storage = AsyncMock()
    chat = AsyncMock()
    svc = ProjectService(storage=storage, chat_service=chat)
    return svc, storage, chat


def test_create_allowed_for_any_user():
    async def go():
        svc, storage, _ = make_service()
        storage.create = AsyncMock(side_effect=lambda p: p)
        dep = uuid4()
        user = User(id=uuid4(), org_id=uuid4(), name="u", username="u", email="u@u",
                    department_id=dep)
        p = await svc.create("P", user)
        assert p.name == "P"
        assert storage.create.call_args[0][0].department_id == dep  # frozen from creator
    asyncio.run(go())


def test_create_head_can():
    async def go():
        svc, storage, _ = make_service()
        storage.create = AsyncMock(side_effect=lambda p: p)
        p = await svc.create("P", make_user(is_head=True))
        assert p.name == "P"
    asyncio.run(go())


def test_create_admin_can():
    async def go():
        svc, storage, _ = make_service()
        storage.create = AsyncMock(side_effect=lambda p: p)
        p = await svc.create("P2", make_user(is_admin=True))
        assert p.name == "P2"
    asyncio.run(go())


def test_create_empty_name_rejected():
    async def go():
        svc, storage, _ = make_service()
        with pytest.raises(ValueError):
            await svc.create("   ", make_user(is_admin=True))
    asyncio.run(go())


def test_list_by_org_include_archived_passthrough():
    async def go():
        svc, storage, _ = make_service()
        storage.list_by_org = AsyncMock(return_value=[])
        org_id = uuid4()
        await svc.list_by_org(org_id, include_archived=True)
        storage.list_by_org.assert_called_once_with(org_id, True)
    asyncio.run(go())


def test_get_cross_org_returns_none():
    async def go():
        svc, storage, _ = make_service()
        user = make_user(is_admin=True)
        foreign = Project(org_id=uuid4(), name="X")  # different org
        storage.get_by_id = AsyncMock(return_value=foreign)
        result = await svc.get(foreign.id, user)
        assert result is None
    asyncio.run(go())


def test_get_same_org_returns_project():
    async def go():
        svc, storage, _ = make_service()
        user = make_user(is_admin=True)
        mine = Project(org_id=user.org_id, name="Mine")
        storage.get_by_id = AsyncMock(return_value=mine)
        result = await svc.get(mine.id, user)
        assert result is mine
    asyncio.run(go())


def test_modify_by_creator_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        creator = User(id=uuid4(), org_id=org, name="c", username="c", email="c@u")
        proj = Project(org_id=org, name="P", created_by_user_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        out = await svc.update(proj.id, creator, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_admin_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        admin = User(id=uuid4(), org_id=org, name="a", username="a", email="a@u", is_admin=True)
        out = await svc.update(proj.id, admin, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_head_same_department_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4(); dep = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=dep)
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=dep)
        out = await svc.update(proj.id, head, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_head_other_department_forbidden():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=uuid4())
        with pytest.raises(PermissionError):
            await svc.update(proj.id, head, name="P2")
    asyncio.run(go())


def test_modify_by_unrelated_user_forbidden():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        other = User(id=uuid4(), org_id=org, name="o", username="o", email="o@u")
        with pytest.raises(PermissionError):
            await svc.update(proj.id, other, name="P2")
    asyncio.run(go())


def test_modify_by_null_dept_head_on_null_dept_project_forbidden():
    # Privilege-hole guard: a head with no department must NOT be able to modify
    # a project with no department (NULL == NULL must not pass the head branch).
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=None)
        storage.get_by_id = AsyncMock(return_value=proj)
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=None)
        with pytest.raises(PermissionError):
            await svc.update(proj.id, head, name="P2")
    asyncio.run(go())


def test_list_visible_admin_lists_whole_org():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        storage.list_by_org = AsyncMock(return_value=[])
        storage.list_visible_for_user = AsyncMock(return_value=[])
        admin = User(id=uuid4(), org_id=org, name="a", username="a", email="a@u", is_admin=True)
        await svc.list_visible(admin, include_archived=False)
        storage.list_by_org.assert_awaited_once()
        storage.list_visible_for_user.assert_not_awaited()
    asyncio.run(go())


def test_list_visible_head_passes_department():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4(); dep = uuid4()
        storage.list_visible_for_user = AsyncMock(return_value=[])
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=dep)
        await svc.list_visible(head, include_archived=False)
        kwargs = storage.list_visible_for_user.call_args.kwargs
        assert kwargs.get("department_id") == dep
    asyncio.run(go())


def test_list_visible_regular_no_department():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        storage.list_visible_for_user = AsyncMock(return_value=[])
        reg = User(id=uuid4(), org_id=org, name="r", username="r", email="r@u")
        await svc.list_visible(reg, include_archived=False)
        kwargs = storage.list_visible_for_user.call_args.kwargs
        assert kwargs.get("department_id") is None
    asyncio.run(go())


def test_delete_archives_chat():
    async def go():
        svc, storage, chat = make_service()
        user = make_user(is_admin=True)
        proj = Project(org_id=user.org_id, name="X")
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.deactivate = AsyncMock(return_value=True)
        chat.archive_project_chat = AsyncMock()
        ok = await svc.delete(proj.id, user)
        assert ok is True
        storage.deactivate.assert_called_once_with(proj.id)
        chat.archive_project_chat.assert_called_once_with(proj.id)
    asyncio.run(go())


def test_delete_missing_returns_false():
    async def go():
        svc, storage, _ = make_service()
        storage.get_by_id = AsyncMock(return_value=None)
        ok = await svc.delete(uuid4(), make_user(is_admin=True))
        assert ok is False
    asyncio.run(go())


def test_delete_cross_org_rejected():
    async def go():
        svc, storage, _ = make_service()
        user = make_user(is_admin=True)
        foreign = Project(org_id=uuid4(), name="X")
        storage.get_by_id = AsyncMock(return_value=foreign)
        with pytest.raises(ValueError):
            await svc.delete(foreign.id, user)
    asyncio.run(go())
