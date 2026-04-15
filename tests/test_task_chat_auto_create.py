"""
Tests for auto-creation of task chats on TaskService.create (item 11).
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_service import TaskService


def make_user(is_admin=False, org_id=None):
    return User(
        id=uuid4(), org_id=org_id or uuid4(),
        name="u", username="u", email="u@u",
        is_admin=is_admin,
    )


def make_service():
    return TaskService(
        storage=AsyncMock(),
        in_app_notification_service=AsyncMock(),
        chat_service=AsyncMock(),
        task_event_service=AsyncMock(),
        project_service=AsyncMock(),
    )


def test_create_task_triggers_chat_creation():
    async def go():
        svc = make_service()
        svc.storage.create = AsyncMock(side_effect=lambda t: t)
        svc.chat_service.create_task_chat = AsyncMock()
        creator = make_user()
        assignee = make_user(org_id=creator.org_id)
        task = await svc.create(
            org_id=creator.org_id, title="T",
            assignee_user_id=assignee.id, created_by_user_id=creator.id,
        )
        svc.chat_service.create_task_chat.assert_called_once()
        kwargs = svc.chat_service.create_task_chat.await_args.kwargs
        assert kwargs["task_id"] == task.id
        assert kwargs["creator_id"] == creator.id
        assert kwargs["assignee_id"] == assignee.id
    asyncio.run(go())


def test_create_task_chat_uses_assignee_as_creator_when_creator_missing():
    async def go():
        svc = make_service()
        svc.storage.create = AsyncMock(side_effect=lambda t: t)
        svc.chat_service.create_task_chat = AsyncMock()
        org = uuid4()
        assignee = make_user(org_id=org)
        await svc.create(
            org_id=org, title="T",
            assignee_user_id=assignee.id, created_by_user_id=None,
        )
        kwargs = svc.chat_service.create_task_chat.await_args.kwargs
        assert kwargs["creator_id"] == assignee.id
    asyncio.run(go())


def test_reassign_task_adds_new_assignee_to_chat():
    async def go():
        svc = make_service()
        svc.chat_service.add_task_chat_participant = AsyncMock()
        creator = make_user()
        old_assignee = make_user(org_id=creator.org_id)
        new_assignee = make_user(org_id=creator.org_id)
        task = Task(
            org_id=creator.org_id,
            title="T",
            assignee_user_id=old_assignee.id,
            created_by_user_id=creator.id,
        )
        svc.storage.get_by_id = AsyncMock(return_value=task)
        svc.storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.update(
            task_id=task.id,
            assignee_user_id=new_assignee.id,
            actor_user_id=creator.id,
        )
        svc.chat_service.add_task_chat_participant.assert_called_once_with(
            task.id, new_assignee.id,
        )
    asyncio.run(go())


def test_reassign_records_assignee_changed_event():
    async def go():
        svc = make_service()
        creator = make_user()
        old = make_user(org_id=creator.org_id)
        new = make_user(org_id=creator.org_id)
        task = Task(
            org_id=creator.org_id, title="T",
            assignee_user_id=old.id, created_by_user_id=creator.id,
        )
        svc.storage.get_by_id = AsyncMock(return_value=task)
        svc.storage.update = AsyncMock(side_effect=lambda t: t)
        svc.task_event_service.record = AsyncMock()
        await svc.update(task_id=task.id, assignee_user_id=new.id, actor_user_id=creator.id)
        types = [c.kwargs["event_type"] for c in svc.task_event_service.record.await_args_list]
        assert "assignee_changed" in types
    asyncio.run(go())


def test_chat_creation_failure_does_not_block_task_creation():
    """If chat creation raises, the task itself must still be returned."""
    async def go():
        svc = make_service()
        svc.storage.create = AsyncMock(side_effect=lambda t: t)
        svc.chat_service.create_task_chat = AsyncMock(side_effect=RuntimeError("boom"))
        creator = make_user()
        assignee = make_user(org_id=creator.org_id)
        task = await svc.create(
            org_id=creator.org_id, title="T",
            assignee_user_id=assignee.id, created_by_user_id=creator.id,
        )
        assert task is not None
        assert task.title == "T"
    asyncio.run(go())
