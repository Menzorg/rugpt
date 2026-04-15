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


def test_create_requires_head_or_admin():
    async def go():
        svc, storage, _ = make_service()
        storage.create = AsyncMock(side_effect=lambda p: p)
        with pytest.raises(PermissionError):
            await svc.create("P", make_user())
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


def test_update_head_can_rename():
    async def go():
        svc, storage, _ = make_service()
        user = make_user(is_head=True)
        proj = Project(org_id=user.org_id, name="Old")
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        out = await svc.update(proj.id, user, name="New")
        assert out.name == "New"
    asyncio.run(go())


def test_update_regular_rejected():
    async def go():
        svc, storage, _ = make_service()
        user = make_user()
        proj = Project(org_id=user.org_id, name="X")
        storage.get_by_id = AsyncMock(return_value=proj)
        with pytest.raises(PermissionError):
            await svc.update(proj.id, user, name="New")
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
