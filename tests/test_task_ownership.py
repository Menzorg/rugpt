"""
Tests for TaskService ownership and prioritization (item 9).

Mocks task_storage and in_app_notification_service. Tests all branches:
- priority computation by creator role
- create sets created_by_user_id
- assignee can take and mark-done
- only creator can accept/reject
- deadline negotiation flow
- legacy task without creator
"""
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4, UUID

from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_service import TaskService, compute_priority


def make_user(user_id=None, is_admin=False, is_head=False, name="Test"):
    return User(
        id=user_id or uuid4(),
        org_id=uuid4(),
        name=name,
        username=name.lower(),
        email=f"{name.lower()}@test.com",
        is_admin=is_admin,
        is_head=is_head,
    )


def make_task(
    task_id=None, status="created", assignee_id=None, creator_id=None,
    awaiting_review_at=None, proposed_deadline=None, proposed_by=None,
):
    return Task(
        id=task_id or uuid4(),
        org_id=uuid4(),
        title="Test task",
        status=status,
        assignee_user_id=assignee_id or uuid4(),
        created_by_user_id=creator_id,
        awaiting_review_at=awaiting_review_at,
        proposed_deadline=proposed_deadline,
        proposed_deadline_by=proposed_by,
    )


def make_service():
    storage = AsyncMock()
    notif = AsyncMock()
    return TaskService(storage, notif), storage, notif


# === Priority computation ===

def test_priority_admin():
    creator = {"is_admin": True, "is_head": False}
    assert compute_priority(creator) == 3


def test_priority_head():
    creator = {"is_admin": False, "is_head": True}
    assert compute_priority(creator) == 2


def test_priority_regular():
    creator = {"is_admin": False, "is_head": False}
    assert compute_priority(creator) == 1


def test_priority_legacy_none():
    assert compute_priority(None) == 1


# === Create task sets created_by_user_id ===

def test_create_sets_created_by():
    async def go():
        svc, storage, notif = make_service()
        creator = make_user()
        assignee = make_user()
        storage.create = AsyncMock(side_effect=lambda t: t)

        task = await svc.create(
            org_id=creator.org_id,
            title="Hello",
            assignee_user_id=assignee.id,
            created_by_user_id=creator.id,
        )

        assert task.created_by_user_id == creator.id
        assert task.assignee_user_id == assignee.id
        storage.create.assert_called_once()

    asyncio.run(go())


# === Status transitions: assignee actions ===

def test_take_assignee_only():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="created", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.take_task(task.id, assignee)
        assert result.status == "in_progress"

        # Wrong user can't take
        other = make_user()
        with pytest.raises(PermissionError):
            await svc.take_task(task.id, other)

    asyncio.run(go())


def test_mark_done_sets_awaiting_review():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="in_progress", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.mark_done(task.id, assignee)
        assert result.status == "awaiting_review"
        assert result.awaiting_review_at is not None

    asyncio.run(go())


def test_assignee_cannot_set_done():
    """Assignee cannot accept their own work — only creator can."""
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(PermissionError):
            await svc.accept_task(task.id, assignee)

    asyncio.run(go())


# === Status transitions: creator actions ===

def test_creator_accepts_awaiting_review():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.accept_task(task.id, creator)
        assert result.status == "done"

    asyncio.run(go())


def test_creator_rejects_returns_to_in_progress():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(status="awaiting_review", assignee_id=assignee.id, creator_id=creator.id, awaiting_review_at=datetime.utcnow())
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        result = await svc.reject_task(task.id, creator, comment="Не хватает раздела X")
        assert result.status == "in_progress"
        assert result.awaiting_review_at is None

    asyncio.run(go())


# === Deadline negotiation ===

def test_deadline_negotiation_flow():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        original_deadline = datetime(2026, 4, 20, 18, 0)
        proposed = datetime(2026, 4, 25, 18, 0)
        task = make_task(status="in_progress", assignee_id=assignee.id, creator_id=creator.id)
        task.deadline = original_deadline

        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        # Assignee proposes
        result = await svc.propose_deadline(task.id, assignee, proposed)
        assert result.proposed_deadline == proposed
        assert result.proposed_deadline_by == assignee.id

        # Creator accepts
        result2 = await svc.accept_proposed_deadline(task.id, creator)
        assert result2.deadline == proposed
        assert result2.proposed_deadline is None
        assert result2.proposed_deadline_by is None

    asyncio.run(go())


def test_assignee_cannot_set_deadline_directly():
    async def go():
        svc, storage, notif = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee_id=assignee.id, creator_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(PermissionError):
            await svc.set_deadline(task.id, assignee, datetime.utcnow() + timedelta(days=7))

    asyncio.run(go())


# === Legacy task without creator ===

def test_legacy_task_no_creator_priority_one():
    """Legacy task without creator gets priority 1."""
    assert compute_priority(None) == 1


def test_legacy_task_only_admin_can_manage():
    """Legacy tasks (created_by NULL) can only be managed by admin."""
    async def go():
        svc, storage, notif = make_service()
        admin = make_user(is_admin=True)
        regular = make_user()
        task = make_task(status="awaiting_review", creator_id=None)
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        # Regular user cannot accept
        with pytest.raises(PermissionError):
            await svc.accept_task(task.id, regular)

        # Admin can accept
        result = await svc.accept_task(task.id, admin)
        assert result.status == "done"

    asyncio.run(go())


def test_admin_overrides_creator_check():
    """Admin can accept/reject any task even when not the creator."""
    async def go():
        svc, storage, notif = make_service()
        admin = make_user(is_admin=True)
        regular_creator = make_user()
        assignee = make_user()
        task = make_task(
            status="awaiting_review",
            assignee_id=assignee.id,
            creator_id=regular_creator.id,
        )
        storage.get_by_id = AsyncMock(return_value=task)
        storage.update = AsyncMock(side_effect=lambda t: t)

        # Admin (not the creator) can accept
        result = await svc.accept_task(task.id, admin)
        assert result.status == "done"

    asyncio.run(go())
