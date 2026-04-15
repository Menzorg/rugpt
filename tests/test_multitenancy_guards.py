"""
Multi-tenancy guards for item 11 (projects + tasks).
"""
import asyncio
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.project import Project
from src.engine.models.user import User
from src.engine.services.project_service import ProjectService
from src.engine.services.task_service import TaskService


def make_user(is_admin=False, org_id=None):
    return User(
        id=uuid4(), org_id=org_id or uuid4(),
        name="u", username="u", email="u@u", is_admin=is_admin,
    )


def test_project_service_get_hides_cross_org():
    async def go():
        storage = AsyncMock()
        svc = ProjectService(storage, AsyncMock())
        user = make_user(is_admin=True)
        foreign = Project(org_id=uuid4(), name="Foreign")
        storage.get_by_id = AsyncMock(return_value=foreign)
        assert await svc.get(foreign.id, user) is None
    asyncio.run(go())


def test_project_service_update_cross_org_rejected():
    async def go():
        storage = AsyncMock()
        svc = ProjectService(storage, AsyncMock())
        user = make_user(is_admin=True)
        foreign = Project(org_id=uuid4(), name="F")
        storage.get_by_id = AsyncMock(return_value=foreign)
        with pytest.raises(ValueError):
            await svc.update(foreign.id, user, name="X")
    asyncio.run(go())


def test_task_create_with_foreign_project_rejected():
    async def go():
        svc = TaskService(
            storage=AsyncMock(),
            in_app_notification_service=AsyncMock(),
            chat_service=AsyncMock(),
            task_event_service=AsyncMock(),
            project_service=AsyncMock(),
        )
        svc.project_service.storage = AsyncMock()
        org = uuid4()
        foreign_proj = Project(org_id=uuid4(), name="F")  # other org
        svc.project_service.storage.get_by_id = AsyncMock(return_value=foreign_proj)
        with pytest.raises(ValueError):
            await svc.create(
                org_id=org, title="T",
                assignee_user_id=uuid4(),
                created_by_user_id=uuid4(),
                project_id=foreign_proj.id,
            )
    asyncio.run(go())


def test_task_create_with_archived_project_rejected():
    async def go():
        svc = TaskService(
            storage=AsyncMock(),
            in_app_notification_service=AsyncMock(),
            chat_service=AsyncMock(),
            task_event_service=AsyncMock(),
            project_service=AsyncMock(),
        )
        svc.project_service.storage = AsyncMock()
        org = uuid4()
        archived = Project(org_id=org, name="A", is_active=False)
        svc.project_service.storage.get_by_id = AsyncMock(return_value=archived)
        with pytest.raises(ValueError):
            await svc.create(
                org_id=org, title="T",
                assignee_user_id=uuid4(),
                created_by_user_id=uuid4(),
                project_id=archived.id,
            )
    asyncio.run(go())
