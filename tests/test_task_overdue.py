"""
Tests for overdue-as-overlay-flag (deadline management of overdue tasks).

Covers:
- check_overdue sets is_overdue WITHOUT touching the work status
- assignee can still take/mark_done an overdue task (work not frozen)
- set_deadline requires a manager (creator / dept head of creator / admin) and
  clears is_overdue when rescheduled to the future
- a plain assignee (non-manager) cannot set the deadline
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_service import TaskService


def make_user(uid=None, is_admin=False, is_head=False, dept=None):
    return User(
        id=uid or uuid4(), org_id=uuid4(), name="U", username="u",
        email=f"{uuid4()}@t.com", is_admin=is_admin, is_head=is_head, department_id=dept,
    )


def make_task(status="in_progress", assignee=None, creator=None, deadline=None, is_overdue=False):
    return Task(
        id=uuid4(), org_id=uuid4(), title="T", status=status,
        assignee_user_id=assignee or uuid4(), created_by_user_id=creator,
        deadline=deadline, is_overdue=is_overdue,
    )


def make_service():
    storage = AsyncMock()
    notif = AsyncMock()
    return TaskService(storage, notif), storage, notif


def test_check_overdue_sets_flag_not_status():
    async def go():
        svc, storage, _ = make_service()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        t = make_task(status="in_progress", deadline=past, is_overdue=False)
        storage.list_active_with_deadline = AsyncMock(return_value=[t])
        storage.update = AsyncMock(side_effect=lambda x: x)

        result = await svc.check_overdue()

        assert t.is_overdue is True
        assert t.status == "in_progress"   # work status preserved
        assert t in result

    asyncio.run(go())


def test_check_overdue_skips_done_and_already_flagged():
    async def go():
        svc, storage, _ = make_service()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        done = make_task(status="done", deadline=past)
        already = make_task(status="in_progress", deadline=past, is_overdue=True)
        storage.list_active_with_deadline = AsyncMock(return_value=[done, already])
        storage.update = AsyncMock(side_effect=lambda x: x)

        result = await svc.check_overdue()

        assert done.is_overdue is False
        assert result == []  # nothing newly flagged

    asyncio.run(go())


def test_assignee_can_take_overdue_task():
    """Core requirement: an overdue task must stay workable by the assignee."""
    async def go():
        svc, storage, _ = make_service()
        assignee = make_user()
        t = make_task(status="created", assignee=assignee.id, is_overdue=True)
        storage.get_by_id = AsyncMock(return_value=t)
        storage.update = AsyncMock(side_effect=lambda x: x)

        updated = await svc.take_task(t.id, assignee)

        assert updated.status == "in_progress"
        assert updated.is_overdue is True  # overlay flag preserved

    asyncio.run(go())


def test_assignee_can_mark_done_overdue_task():
    async def go():
        svc, storage, _ = make_service()
        assignee = make_user()
        t = make_task(status="in_progress", assignee=assignee.id, is_overdue=True)
        storage.get_by_id = AsyncMock(return_value=t)
        storage.update = AsyncMock(side_effect=lambda x: x)

        updated = await svc.mark_done(t.id, assignee)

        assert updated.status == "awaiting_review"

    asyncio.run(go())


def test_set_deadline_by_creator_clears_overdue():
    async def go():
        svc, storage, _ = make_service()
        creator = make_user()
        t = make_task(
            status="in_progress", creator=creator.id,
            deadline=datetime.now(timezone.utc) - timedelta(days=1), is_overdue=True,
        )
        storage.get_by_id = AsyncMock(return_value=t)
        storage.update = AsyncMock(side_effect=lambda x: x)

        future = datetime.now(timezone.utc) + timedelta(days=3)
        updated = await svc.set_deadline(t.id, creator, future)

        assert updated.deadline == future
        assert updated.is_overdue is False

    asyncio.run(go())


def test_set_deadline_denied_for_plain_assignee():
    """Employees cannot shift their own deadline without approval."""
    async def go():
        svc, storage, _ = make_service()
        creator = make_user()
        assignee = make_user()  # not creator, not head, not admin
        t = make_task(creator=creator.id, assignee=assignee.id, is_overdue=True)
        storage.get_by_id = AsyncMock(return_value=t)
        try:
            await svc.set_deadline(t.id, assignee, datetime.now(timezone.utc) + timedelta(days=1))
            assert False, "expected PermissionError"
        except PermissionError:
            pass

    asyncio.run(go())


def test_set_deadline_allowed_for_dept_head_of_creator():
    async def go():
        svc, storage, _ = make_service()
        dept = uuid4()
        creator = make_user(dept=dept)
        head = make_user(is_head=True, dept=dept)
        user_storage = AsyncMock()
        user_storage.get_by_id = AsyncMock(return_value=creator)
        svc.user_storage = user_storage
        t = make_task(creator=creator.id, is_overdue=True)
        storage.get_by_id = AsyncMock(return_value=t)
        storage.update = AsyncMock(side_effect=lambda x: x)

        updated = await svc.set_deadline(
            t.id, head, datetime.now(timezone.utc) + timedelta(days=2),
        )
        assert updated.is_overdue is False

    asyncio.run(go())


def test_set_deadline_denied_for_head_of_other_department():
    async def go():
        svc, storage, _ = make_service()
        creator = make_user(dept=uuid4())
        head = make_user(is_head=True, dept=uuid4())  # different department
        user_storage = AsyncMock()
        user_storage.get_by_id = AsyncMock(return_value=creator)
        svc.user_storage = user_storage
        t = make_task(creator=creator.id, is_overdue=True)
        storage.get_by_id = AsyncMock(return_value=t)
        try:
            await svc.set_deadline(t.id, head, datetime.now(timezone.utc) + timedelta(days=1))
            assert False, "expected PermissionError"
        except PermissionError:
            pass

    asyncio.run(go())
