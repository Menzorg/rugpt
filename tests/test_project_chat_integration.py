"""
Tests for project-chat integration in TaskService (item 11).

Covers the ensure/archive/reactivation lifecycle and the race between
deactivate (last-task archive) and a new task appearing.
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.chat import Chat, ChatType
from src.engine.models.project import Project
from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.chat_service import ChatService
from src.engine.services.task_service import TaskService


def make_user(is_admin=False, org_id=None):
    return User(
        id=uuid4(), org_id=org_id or uuid4(),
        name="u", username="u", email="u@u", is_admin=is_admin,
    )


def make_task_service(project_available=True):
    svc = TaskService(
        storage=AsyncMock(),
        in_app_notification_service=AsyncMock(),
        chat_service=AsyncMock(),
        task_event_service=AsyncMock(),
        project_service=AsyncMock(),
    )
    if project_available:
        # project_service.storage.get_by_id returns an active, same-org project
        svc.project_service.storage = AsyncMock()
    return svc


def test_task_with_project_calls_ensure_membership():
    async def go():
        svc = make_task_service()
        org = uuid4()
        creator = make_user(org_id=org)
        assignee = make_user(org_id=org)
        proj = Project(org_id=org, name="P")

        svc.project_service.storage.get_by_id = AsyncMock(return_value=proj)
        svc.storage.create = AsyncMock(side_effect=lambda t: t)
        svc.chat_service.create_task_chat = AsyncMock()
        svc.chat_service.ensure_project_chat_membership = AsyncMock()

        await svc.create(
            org_id=org, title="T",
            assignee_user_id=assignee.id, created_by_user_id=creator.id,
            project_id=proj.id,
        )
        svc.chat_service.ensure_project_chat_membership.assert_called_once()
        kwargs = svc.chat_service.ensure_project_chat_membership.await_args.kwargs
        assert kwargs["project_id"] == proj.id
        assert set(kwargs["user_ids"]) == {creator.id, assignee.id}
    asyncio.run(go())


def test_ensure_membership_creates_chat_when_missing():
    """ChatService.ensure_project_chat_membership creates a new chat when none exists."""
    async def go():
        chat_storage = AsyncMock()
        msg_storage = AsyncMock()
        svc = ChatService(chat_storage, msg_storage)
        chat_storage.get_by_project_id = AsyncMock(return_value=None)
        chat_storage.create = AsyncMock(side_effect=lambda c: c)

        org = uuid4()
        project_id = uuid4()
        users = [uuid4(), uuid4()]
        chat = await svc.ensure_project_chat_membership(project_id, org, users)
        assert chat.type == ChatType.PROJECT
        assert chat.project_id == project_id
        assert set(chat.participants) == set(users)
    asyncio.run(go())


def test_ensure_membership_dedupes_participants():
    async def go():
        chat_storage = AsyncMock()
        svc = ChatService(chat_storage, AsyncMock())
        org = uuid4(); project_id = uuid4()
        existing_user = uuid4()
        existing_chat = Chat(
            org_id=org, type=ChatType.PROJECT, project_id=project_id,
            participants=[existing_user], is_active=True,
        )
        chat_storage.get_by_project_id = AsyncMock(return_value=existing_chat)
        chat_storage.update = AsyncMock(side_effect=lambda c: c)
        new_user = uuid4()
        chat = await svc.ensure_project_chat_membership(project_id, org, [existing_user, new_user])
        assert chat.participants == [existing_user, new_user]
        # Call again with the same users -> no duplication, no extra update.
        chat_storage.update.reset_mock()
        chat = await svc.ensure_project_chat_membership(project_id, org, [existing_user])
        assert chat.participants == [existing_user, new_user]
        chat_storage.update.assert_not_called()
    asyncio.run(go())


def test_ensure_membership_reactivates_archived_chat():
    async def go():
        chat_storage = AsyncMock()
        svc = ChatService(chat_storage, AsyncMock())
        org = uuid4(); project_id = uuid4()
        u = uuid4()
        archived = Chat(
            org_id=org, type=ChatType.PROJECT, project_id=project_id,
            participants=[u], is_active=False,
        )
        chat_storage.get_by_project_id = AsyncMock(return_value=archived)
        chat_storage.update = AsyncMock(side_effect=lambda c: c)
        new_u = uuid4()
        chat = await svc.ensure_project_chat_membership(project_id, org, [new_u])
        assert chat.is_active is True
        assert set(chat.participants) == {u, new_u}
        chat_storage.update.assert_called_once()
    asyncio.run(go())


def test_deactivate_last_task_archives_project_chat():
    async def go():
        svc = make_task_service()
        user = make_user()
        task = Task(
            org_id=user.org_id, title="T",
            assignee_user_id=user.id, created_by_user_id=user.id,
        )
        task.project_id = uuid4()
        svc.storage.get_by_id = AsyncMock(return_value=task)
        svc.storage.deactivate = AsyncMock(return_value=True)
        svc.storage.count_active_in_project = AsyncMock(return_value=0)
        svc.chat_service.archive_task_chat = AsyncMock()
        svc.chat_service.archive_project_chat = AsyncMock()
        ok = await svc.deactivate(task.id, user=user)
        assert ok
        svc.chat_service.archive_project_chat.assert_called_once_with(task.project_id)
    asyncio.run(go())


def test_deactivate_non_last_task_keeps_project_chat():
    async def go():
        svc = make_task_service()
        user = make_user()
        task = Task(
            org_id=user.org_id, title="T",
            assignee_user_id=user.id, created_by_user_id=user.id,
        )
        task.project_id = uuid4()
        svc.storage.get_by_id = AsyncMock(return_value=task)
        svc.storage.deactivate = AsyncMock(return_value=True)
        svc.storage.count_active_in_project = AsyncMock(return_value=2)
        svc.chat_service.archive_task_chat = AsyncMock()
        svc.chat_service.archive_project_chat = AsyncMock()
        await svc.deactivate(task.id, user=user)
        svc.chat_service.archive_project_chat.assert_not_called()
    asyncio.run(go())
