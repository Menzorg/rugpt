"""
Tests for TaskService audit-trail integration (item 11).

Uses mocked storage + mocked TaskEventService/ChatService/ProjectService.
Verifies that each status transition writes the expected task_event.
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_service import TaskService


def make_user(uid=None, is_admin=False, is_head=False, org_id=None):
    return User(
        id=uid or uuid4(),
        org_id=org_id or uuid4(),
        name="u",
        username="u",
        email="u@u",
        is_admin=is_admin,
        is_head=is_head,
    )


def make_task(status="created", assignee=None, creator=None, org_id=None):
    return Task(
        id=uuid4(),
        org_id=org_id or uuid4(),
        title="T",
        status=status,
        assignee_user_id=assignee or uuid4(),
        created_by_user_id=creator,
    )


def make_service():
    storage = AsyncMock()
    notif = AsyncMock()
    chat = AsyncMock()
    event = AsyncMock()
    event.record = AsyncMock()
    project = AsyncMock()
    svc = TaskService(
        storage=storage,
        in_app_notification_service=notif,
        chat_service=chat,
        task_event_service=event,
        project_service=project,
    )
    return svc, storage, chat, event


def test_create_records_created_event():
    async def go():
        svc, storage, _, event = make_service()
        creator = make_user()
        assignee = make_user(org_id=creator.org_id)
        storage.create = AsyncMock(side_effect=lambda t: t)
        await svc.create(
            org_id=creator.org_id, title="T",
            assignee_user_id=assignee.id, created_by_user_id=creator.id,
        )
        call = event.record.await_args_list[0]
        assert call.kwargs["event_type"] == "created"
        assert call.kwargs["actor_user_id"] == creator.id
    asyncio.run(go())


def test_take_records_took_event():
    async def go():
        svc, storage, _, event = make_service()
        assignee = make_user()
        task = make_task(status="created", assignee=assignee.id, creator=uuid4())
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.take_task(task.id, assignee)
        event.record.assert_called()
        kwargs = event.record.await_args.kwargs
        assert kwargs["event_type"] == "took"
        assert kwargs["payload"]["from_status"] == "created"
        assert kwargs["payload"]["to_status"] == "in_progress"
    asyncio.run(go())


def test_mark_done_records_marked_done_event():
    async def go():
        svc, storage, _, event = make_service()
        assignee = make_user()
        task = make_task(status="in_progress", assignee=assignee.id, creator=uuid4())
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.mark_done(task.id, assignee)
        assert event.record.await_args.kwargs["event_type"] == "marked_done"
    asyncio.run(go())


def test_accept_records_accepted_event():
    async def go():
        svc, storage, _, event = make_service()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee=uuid4(), creator=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.accept_task(task.id, creator)
        assert event.record.await_args.kwargs["event_type"] == "accepted"
    asyncio.run(go())


def test_reject_records_rejected_with_comment():
    async def go():
        svc, storage, _, event = make_service()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee=uuid4(), creator=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.reject_task(task.id, creator, comment="bad")
        kwargs = event.record.await_args.kwargs
        assert kwargs["event_type"] == "rejected"
        assert kwargs["payload"]["comment"] == "bad"
    asyncio.run(go())


def test_set_deadline_records_event_with_old_new():
    async def go():
        svc, storage, _, event = make_service()
        creator = make_user()
        task = make_task(status="in_progress", assignee=uuid4(), creator=creator.id)
        task.deadline = datetime(2026, 4, 20)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        new = datetime(2026, 4, 25)
        await svc.set_deadline(task.id, creator, new)
        kwargs = event.record.await_args.kwargs
        assert kwargs["event_type"] == "deadline_set"
        assert kwargs["payload"]["old"] == "2026-04-20T00:00:00"
        assert kwargs["payload"]["new"] == "2026-04-25T00:00:00"
    asyncio.run(go())


def test_deadline_proposal_accept_records_event():
    async def go():
        svc, storage, _, event = make_service()
        creator = make_user()
        assignee = make_user()
        task = make_task(status="in_progress", assignee=assignee.id, creator=creator.id)
        task.proposed_deadline = datetime(2026, 5, 1)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)
        await svc.accept_proposed_deadline(task.id, creator)
        kwargs = event.record.await_args.kwargs
        assert kwargs["event_type"] == "deadline_proposal_accepted"
    asyncio.run(go())


def test_deactivate_records_cancelled_event():
    async def go():
        svc, storage, chat, event = make_service()
        creator = make_user()
        task = make_task(assignee=uuid4(), creator=creator.id, org_id=creator.org_id)
        task.project_id = None
        storage.get_by_id = AsyncMock(return_value=task)
        storage.deactivate = AsyncMock(return_value=True)
        chat.archive_task_chat = AsyncMock()
        ok = await svc.deactivate(task.id, user=creator)
        assert ok is True
        chat.archive_task_chat.assert_called_once_with(task.id)
        types = [c.kwargs["event_type"] for c in event.record.await_args_list]
        assert "cancelled" in types
    asyncio.run(go())
