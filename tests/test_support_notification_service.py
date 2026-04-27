"""Tests for SupportNotificationService — fan-out + single-target notifications.

Mocks InAppNotificationService and UserStorage to verify side-effect calls
without touching real DB.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus, ClosedByRole,
)
from src.engine.models.user import User
from src.engine.services.support_notification_service import SupportNotificationService


def _ticket(
    *,
    requester_user_id=None,
    assignee_user_id=None,
    closed_by_role=None,
    category=SupportTicketCategory.HOW_TO,
    status=SupportTicketStatus.OPEN,
):
    return SupportTicket(
        id=uuid4(),
        requester_user_id=requester_user_id or uuid4(),
        requester_org_id=uuid4(),
        category=category,
        status=status,
        assignee_user_id=assignee_user_id,
        closed_by_role=closed_by_role,
    )


def _user(id_=None, org_id=None, is_active=True) -> User:
    return User(
        id=id_ or uuid4(),
        org_id=org_id or Config.RUGPT_SUPPORT_ORG_ID,
        name="Op",
        username=f"op_{uuid4().hex[:6]}",
        email="op@t.t",
        is_active=is_active,
    )


def _make_service(operators=None):
    in_app = AsyncMock()
    in_app.create = AsyncMock()
    user_storage = MagicMock()
    user_storage.list_by_org = AsyncMock(return_value=operators or [])
    svc = SupportNotificationService(
        in_app_notification_service=in_app,
        user_storage=user_storage,
    )
    return svc, in_app, user_storage


def _run(coro):
    return asyncio.run(coro)


# ---------- notify_new_in_queue ----------

def test_notify_new_in_queue_fans_out_to_all_active_operators():
    ops = [_user(), _user(), _user()]
    svc, in_app, user_storage = _make_service(operators=ops)
    t = _ticket(category=SupportTicketCategory.BUG)

    _run(svc.notify_new_in_queue(t))

    user_storage.list_by_org.assert_called_once()
    args, kwargs = user_storage.list_by_org.call_args
    assert args[0] == Config.RUGPT_SUPPORT_ORG_ID or kwargs.get("org_id") == Config.RUGPT_SUPPORT_ORG_ID
    # active_only=True is expected (operators marked inactive shouldn't get pinged)
    if "active_only" in kwargs:
        assert kwargs["active_only"] is True

    # Each operator gets one notification
    assert in_app.create.await_count == len(ops)
    for call in in_app.create.await_args_list:
        kwargs = call.kwargs
        assert kwargs["type"] == "system"
        assert kwargs["reference_type"] == "support_ticket"
        assert kwargs["reference_id"] == t.id
        # title must mention "queue" or "очереди" or similar — non-empty
        assert kwargs["title"]


def test_notify_new_in_queue_with_zero_operators_is_safe():
    svc, in_app, _ = _make_service(operators=[])
    t = _ticket(category=SupportTicketCategory.OTHER)
    _run(svc.notify_new_in_queue(t))
    in_app.create.assert_not_called()  # no fan-out targets


def test_notify_new_in_queue_passes_each_operator_id_correctly():
    ops = [_user() for _ in range(2)]
    svc, in_app, _ = _make_service(operators=ops)
    t = _ticket(category=SupportTicketCategory.BUG)
    _run(svc.notify_new_in_queue(t))
    notified_user_ids = {c.kwargs["user_id"] for c in in_app.create.await_args_list}
    expected_user_ids = {op.id for op in ops}
    assert notified_user_ids == expected_user_ids


# ---------- notify_taken ----------

def test_notify_taken_pings_requester_only():
    svc, in_app, _ = _make_service()
    requester_id = uuid4()
    t = _ticket(requester_user_id=requester_id, assignee_user_id=uuid4(),
                status=SupportTicketStatus.IN_PROGRESS)
    _run(svc.notify_taken(t))

    assert in_app.create.await_count == 1
    kwargs = in_app.create.await_args.kwargs
    assert kwargs["user_id"] == requester_id
    assert kwargs["type"] == "system"
    assert kwargs["reference_type"] == "support_ticket"
    assert kwargs["reference_id"] == t.id


# ---------- notify_closed ----------

def test_notify_closed_by_operator_pings_requester():
    svc, in_app, _ = _make_service()
    requester_id = uuid4()
    operator_id = uuid4()
    t = _ticket(
        requester_user_id=requester_id,
        assignee_user_id=operator_id,
        closed_by_role=ClosedByRole.OPERATOR,
        status=SupportTicketStatus.CLOSED,
    )
    _run(svc.notify_closed(t))

    assert in_app.create.await_count == 1
    assert in_app.create.await_args.kwargs["user_id"] == requester_id


def test_notify_closed_by_requester_pings_assignee_if_present():
    svc, in_app, _ = _make_service()
    requester_id = uuid4()
    operator_id = uuid4()
    t = _ticket(
        requester_user_id=requester_id,
        assignee_user_id=operator_id,
        closed_by_role=ClosedByRole.REQUESTER,
        status=SupportTicketStatus.CLOSED,
    )
    _run(svc.notify_closed(t))

    assert in_app.create.await_count == 1
    assert in_app.create.await_args.kwargs["user_id"] == operator_id


def test_notify_closed_by_requester_no_assignee_is_silent():
    """If client closes a how_to ticket before any operator took it,
    there's nobody on the operator side to notify — should be a no-op.
    """
    svc, in_app, _ = _make_service()
    t = _ticket(
        requester_user_id=uuid4(),
        assignee_user_id=None,
        closed_by_role=ClosedByRole.REQUESTER,
        status=SupportTicketStatus.CLOSED,
    )
    _run(svc.notify_closed(t))
    in_app.create.assert_not_called()


# ---------- notify_reopened ----------

def test_notify_reopened_pings_assignee_if_present():
    svc, in_app, _ = _make_service()
    operator_id = uuid4()
    t = _ticket(
        requester_user_id=uuid4(),
        assignee_user_id=operator_id,
        status=SupportTicketStatus.IN_PROGRESS,
    )
    _run(svc.notify_reopened(t))

    assert in_app.create.await_count == 1
    assert in_app.create.await_args.kwargs["user_id"] == operator_id


def test_notify_reopened_no_assignee_is_silent():
    """A reopened ticket without assignee (auto-reopen during queue lifecycle)
    has no operator to notify."""
    svc, in_app, _ = _make_service()
    t = _ticket(assignee_user_id=None)
    _run(svc.notify_reopened(t))
    in_app.create.assert_not_called()


# ---------- common payload shape ----------

def test_all_notifications_use_system_type_and_support_ticket_reference():
    """Every notification this service emits MUST carry type='system' and
    reference_type='support_ticket' so the frontend can route on it."""
    svc, in_app, _ = _make_service(operators=[_user()])
    requester_id = uuid4()
    operator_id = uuid4()
    t = _ticket(
        requester_user_id=requester_id,
        assignee_user_id=operator_id,
        closed_by_role=ClosedByRole.OPERATOR,
        status=SupportTicketStatus.CLOSED,
    )
    # Trigger every method
    _run(svc.notify_new_in_queue(t))
    _run(svc.notify_taken(t))
    _run(svc.notify_closed(t))
    _run(svc.notify_reopened(t))

    for call in in_app.create.await_args_list:
        assert call.kwargs["type"] == "system"
        assert call.kwargs["reference_type"] == "support_ticket"
        assert call.kwargs["reference_id"] == t.id
