"""Tests for support client routes (Task 14).

Unit-level tests against the route handler functions directly. We mock
`get_engine_service` and pass `current_user` dicts that match the real auth
contract (`user_id` and `org_id` keys, both UUIDs).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.engine.config import Config
from src.engine.models.support_ticket import (
    ClosedByRole,
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
)
from src.engine.models.user import User
from src.engine.routes.support import (
    CreateTicketRequest,
    close_ticket,
    create_ticket,
    escalate,
    get_ticket,
    list_my_tickets,
)


# ============================================
# Fixtures / helpers
# ============================================


def _user(id_=None, org_id=None, name="x", email="x@x") -> User:
    return User(
        id=id_ or uuid4(),
        org_id=org_id or uuid4(),
        name=name,
        username="x",
        email=email,
    )


def _ticket(*, requester_user_id=None, requester_org_id=None, **overrides) -> SupportTicket:
    base = dict(
        id=uuid4(),
        requester_user_id=requester_user_id or uuid4(),
        requester_org_id=requester_org_id or uuid4(),
        category=SupportTicketCategory.HOW_TO,
        status=SupportTicketStatus.OPEN,
    )
    base.update(overrides)  # overrides win — lets callers set status/closed_by_role
    return SupportTicket(**base)


def _mock_engine(*, user=None, ticket=None, org=None):
    """Build a MagicMock engine with the storages/services support routes touch."""
    engine = MagicMock()

    engine.user_storage = MagicMock()
    engine.user_storage.get_by_id = AsyncMock(return_value=user)

    engine.support_ticket_storage = MagicMock()
    engine.support_ticket_storage.get_by_id = AsyncMock(return_value=ticket)
    engine.support_ticket_storage.list_by_requester = AsyncMock(return_value=[])

    engine.support_ticket_service = MagicMock()
    engine.support_ticket_service.create_ticket = AsyncMock()
    engine.support_ticket_service.escalate = AsyncMock()
    engine.support_ticket_service.close_ticket = AsyncMock()

    engine.org_storage = MagicMock()
    engine.org_storage.get_by_id = AsyncMock(return_value=org)

    return engine


def _run(coro):
    return asyncio.run(coro)


def _patch_engine(engine):
    return patch(
        "src.engine.routes.support.get_engine_service",
        return_value=engine,
    )


def _current_user(user_id=None, org_id=None) -> dict:
    """Match the real get_current_user contract: UUIDs, keys user_id / org_id."""
    return {
        "user_id": user_id or uuid4(),
        "org_id": org_id or uuid4(),
        "is_admin": False,
        "department_id": None,
        "is_head": False,
    }


# ============================================
# create_ticket
# ============================================


def test_create_ticket_happy_path():
    user = _user()
    fake_ticket = _ticket(requester_user_id=user.id, requester_org_id=user.org_id)
    fake_chat = MagicMock(id=uuid4())

    engine = _mock_engine(user=user)
    engine.support_ticket_service.create_ticket = AsyncMock(
        return_value=(fake_ticket, fake_chat)
    )

    body = CreateTicketRequest(category="how_to", initial_message="как создать чат?")
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        result = _run(create_ticket(body, cu))

    assert result["ticket"]["category"] == "how_to"
    assert result["ticket"]["id"] == str(fake_ticket.id)
    assert result["chat_id"] == str(fake_chat.id)

    # Service was called with the User object (not a dict)
    call_kwargs = engine.support_ticket_service.create_ticket.call_args.kwargs
    assert call_kwargs["requester"] is user
    assert call_kwargs["category"] == SupportTicketCategory.HOW_TO
    assert call_kwargs["initial_message"] == "как создать чат?"


def test_create_ticket_invalid_category_returns_400():
    engine = _mock_engine(user=_user())
    body = CreateTicketRequest(category="garbage", initial_message="x")
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(create_ticket(body, cu))

    assert exc.value.status_code == 400
    assert "garbage" in exc.value.detail
    # Service must not be invoked when validation fails
    engine.support_ticket_service.create_ticket.assert_not_called()


def test_create_ticket_user_not_found_returns_404():
    engine = _mock_engine(user=None)  # user lookup miss
    body = CreateTicketRequest(category="how_to", initial_message="x")
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(create_ticket(body, cu))

    assert exc.value.status_code == 404
    engine.support_ticket_service.create_ticket.assert_not_called()


# ============================================
# list_my_tickets
# ============================================


def test_list_my_tickets_returns_dicts():
    user = _user()
    tickets = [
        _ticket(requester_user_id=user.id),
        _ticket(requester_user_id=user.id),
    ]
    engine = _mock_engine()
    engine.support_ticket_storage.list_by_requester = AsyncMock(return_value=tickets)
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        result = _run(list_my_tickets(status=None, limit=50, current_user=cu))

    assert len(result) == 2
    assert all("category" in t for t in result)

    # Storage called with no status filter
    args, kwargs = engine.support_ticket_storage.list_by_requester.call_args
    assert args[0] == user.id
    assert kwargs["status"] is None
    assert kwargs["limit"] == 50


def test_list_my_tickets_with_status_filter():
    user = _user()
    engine = _mock_engine()
    engine.support_ticket_storage.list_by_requester = AsyncMock(return_value=[])
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        _run(list_my_tickets(status="closed", limit=10, current_user=cu))

    _, kwargs = engine.support_ticket_storage.list_by_requester.call_args
    assert kwargs["status"] == SupportTicketStatus.CLOSED
    assert kwargs["limit"] == 10


def test_list_my_tickets_invalid_status_returns_400():
    engine = _mock_engine()
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(list_my_tickets(status="garbage", limit=50, current_user=cu))

    assert exc.value.status_code == 400


# ============================================
# get_ticket
# ============================================


def test_get_ticket_for_requester_returns_basic_payload():
    user = _user()
    ticket = _ticket(requester_user_id=user.id, requester_org_id=user.org_id)
    engine = _mock_engine(user=user, ticket=ticket)
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        result = _run(get_ticket(ticket.id, cu))

    assert result["id"] == str(ticket.id)
    # No cross-org embed for requester
    assert "requester_name" not in result
    assert "requester_email" not in result
    # Operator-only org_storage lookup must not happen
    engine.org_storage.get_by_id.assert_not_called()


def test_get_ticket_for_operator_includes_cross_org_profile():
    operator_id = uuid4()
    requester_id = uuid4()
    requester_org_id = uuid4()

    ticket = _ticket(
        requester_user_id=requester_id,
        requester_org_id=requester_org_id,
    )

    requester = _user(id_=requester_id, org_id=requester_org_id,
                      name="Иван Клиент", email="ivan@acme.com")

    org = MagicMock()
    org.id = requester_org_id
    org.name = "Acme Corp"

    engine = _mock_engine(user=requester, ticket=ticket, org=org)
    cu = _current_user(user_id=operator_id, org_id=Config.RUGPT_SUPPORT_ORG_ID)

    with _patch_engine(engine):
        result = _run(get_ticket(ticket.id, cu))

    assert result["requester_name"] == "Иван Клиент"
    assert result["requester_email"] == "ivan@acme.com"
    assert result["requester_org_name"] == "Acme Corp"
    assert result["requester_org_id"] == str(requester_org_id)


def test_get_ticket_unauthorized_user_returns_403():
    requester_id = uuid4()
    ticket = _ticket(requester_user_id=requester_id)
    engine = _mock_engine(ticket=ticket)
    intruder = _current_user()  # random user, random non-support org

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket(ticket.id, intruder))

    assert exc.value.status_code == 403


def test_get_ticket_not_found_returns_404():
    engine = _mock_engine(ticket=None)
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket(uuid4(), cu))

    assert exc.value.status_code == 404


# ============================================
# escalate
# ============================================


def test_escalate_happy():
    user = _user()
    ticket = _ticket(requester_user_id=user.id)
    engine = _mock_engine(user=user)
    engine.support_ticket_service.escalate = AsyncMock(return_value=ticket)
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        result = _run(escalate(ticket.id, cu))

    assert result["id"] == str(ticket.id)
    engine.support_ticket_service.escalate.assert_awaited_once_with(ticket.id, user)


def test_escalate_permission_error_returns_403():
    user = _user()
    engine = _mock_engine(user=user)
    engine.support_ticket_service.escalate = AsyncMock(
        side_effect=PermissionError("only requester can escalate"),
    )
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(escalate(uuid4(), cu))

    assert exc.value.status_code == 403


def test_escalate_not_found_returns_404():
    user = _user()
    engine = _mock_engine(user=user)
    engine.support_ticket_service.escalate = AsyncMock(
        side_effect=ValueError("ticket abc not found"),
    )
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(escalate(uuid4(), cu))

    assert exc.value.status_code == 404


def test_escalate_invalid_state_returns_400():
    user = _user()
    engine = _mock_engine(user=user)
    engine.support_ticket_service.escalate = AsyncMock(
        side_effect=ValueError("cannot escalate a closed ticket"),
    )
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(escalate(uuid4(), cu))

    assert exc.value.status_code == 400


# ============================================
# close
# ============================================


def test_close_happy():
    user = _user()
    ticket = _ticket(
        requester_user_id=user.id,
        status=SupportTicketStatus.CLOSED,
        closed_by_role=ClosedByRole.REQUESTER,
    )
    engine = _mock_engine(user=user)
    engine.support_ticket_service.close_ticket = AsyncMock(return_value=ticket)
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine):
        result = _run(close_ticket(ticket.id, cu))

    assert result["status"] == "closed"
    engine.support_ticket_service.close_ticket.assert_awaited_once_with(ticket.id, user)


def test_close_already_closed_returns_400():
    user = _user()
    engine = _mock_engine(user=user)
    engine.support_ticket_service.close_ticket = AsyncMock(
        side_effect=ValueError("ticket already closed or not in closeable state"),
    )
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(close_ticket(uuid4(), cu))

    assert exc.value.status_code == 400


def test_close_permission_error_returns_403():
    user = _user()
    engine = _mock_engine(user=user)
    engine.support_ticket_service.close_ticket = AsyncMock(
        side_effect=PermissionError("not authorized to close this ticket"),
    )
    cu = _current_user(user_id=user.id, org_id=user.org_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(close_ticket(uuid4(), cu))

    assert exc.value.status_code == 403


# ===== Operator routes (Task 15) =====

from src.engine.routes.support import (
    get_queue, take_ticket, get_operator_my,
)


def _operator_user(id_=None) -> dict:
    return {
        "user_id": id_ or uuid4(),
        "org_id": Config.RUGPT_SUPPORT_ORG_ID,
        "is_admin": False,
    }


def _customer_user(id_=None, org_id=None) -> dict:
    return {
        "user_id": id_ or uuid4(),
        "org_id": org_id or uuid4(),
        "is_admin": False,
    }


# ---------- GET /support/queue ----------

def test_get_queue_returns_unassigned_tickets():
    op = _operator_user()
    queue_tickets = [_ticket(), _ticket()]
    engine = _mock_engine()
    engine.support_ticket_storage.list_queue = AsyncMock(return_value=queue_tickets)

    with _patch_engine(engine):
        result = _run(get_queue(limit=100, current_user=op))

    assert len(result) == 2
    engine.support_ticket_storage.list_queue.assert_called_once_with(100)


def test_get_queue_forbidden_for_non_operator():
    customer = _customer_user()
    engine = _mock_engine()
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_queue(limit=100, current_user=customer))
    assert exc.value.status_code == 403


# ---------- POST /support/tickets/{id}/take ----------

def test_take_happy_returns_ticket():
    op_id = uuid4()
    op = _operator_user(id_=op_id)
    operator_user_obj = _user(id_=op_id, org_id=Config.RUGPT_SUPPORT_ORG_ID)
    ticket = _ticket(status=SupportTicketStatus.IN_PROGRESS, assignee_user_id=op_id)
    engine = _mock_engine(user=operator_user_obj)
    engine.support_ticket_service.take_ticket = AsyncMock(return_value=ticket)

    with _patch_engine(engine):
        result = _run(take_ticket(ticket.id, op))

    assert result["status"] == "in_progress"
    assert result["assignee_user_id"] == str(op_id)


def test_take_forbidden_for_non_operator():
    customer = _customer_user()
    engine = _mock_engine(user=_user())
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(take_ticket(uuid4(), customer))
    assert exc.value.status_code == 403


def test_take_already_taken_returns_409():
    op = _operator_user()
    operator_user_obj = _user(id_=op["user_id"], org_id=Config.RUGPT_SUPPORT_ORG_ID)
    engine = _mock_engine(user=operator_user_obj)
    engine.support_ticket_service.take_ticket = AsyncMock(
        side_effect=ValueError("ticket already taken or not in open state")
    )
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(take_ticket(uuid4(), op))
    assert exc.value.status_code == 409


def test_take_not_found_returns_404():
    op = _operator_user()
    operator_user_obj = _user(id_=op["user_id"], org_id=Config.RUGPT_SUPPORT_ORG_ID)
    engine = _mock_engine(user=operator_user_obj)
    engine.support_ticket_service.take_ticket = AsyncMock(
        side_effect=ValueError("ticket not found")
    )
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(take_ticket(uuid4(), op))
    assert exc.value.status_code == 404


def test_take_permission_error_returns_403():
    op = _operator_user()
    operator_user_obj = _user(id_=op["user_id"], org_id=Config.RUGPT_SUPPORT_ORG_ID)
    engine = _mock_engine(user=operator_user_obj)
    engine.support_ticket_service.take_ticket = AsyncMock(
        side_effect=PermissionError("only RuGPT Support operators can take tickets")
    )
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(take_ticket(uuid4(), op))
    assert exc.value.status_code == 403


# ---------- GET /support/operator/my ----------

def test_operator_my_returns_assigned_tickets():
    op_id = uuid4()
    op = _operator_user(id_=op_id)
    assigned = [_ticket(assignee_user_id=op_id), _ticket(assignee_user_id=op_id)]
    engine = _mock_engine()
    engine.support_ticket_storage.list_by_assignee = AsyncMock(return_value=assigned)

    with _patch_engine(engine):
        result = _run(get_operator_my(limit=100, current_user=op))

    assert len(result) == 2
    engine.support_ticket_storage.list_by_assignee.assert_called_once_with(op_id, 100)


def test_operator_my_forbidden_for_non_operator():
    customer = _customer_user()
    engine = _mock_engine()
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_operator_my(limit=100, current_user=customer))
    assert exc.value.status_code == 403


# ===== Common routes (Task 16) =====

from src.engine.routes.support import get_ticket_chat, get_ticket_events


# ---------- GET /support/tickets/{id}/chat ----------

def test_get_ticket_chat_for_requester_returns_chat_id():
    user = _user()
    ticket = _ticket(requester_user_id=user.id)
    fake_chat = MagicMock()
    fake_chat.id = uuid4()
    engine = _mock_engine(user=user, ticket=ticket)
    engine.chat_storage = MagicMock()
    engine.chat_storage.get_by_support_ticket = AsyncMock(return_value=fake_chat)
    current_user = {"user_id": user.id, "org_id": user.org_id}

    with _patch_engine(engine):
        result = _run(get_ticket_chat(ticket.id, current_user))

    assert result == {"chat_id": str(fake_chat.id)}


def test_get_ticket_chat_unauthorized_returns_403():
    requester_id = uuid4()
    ticket = _ticket(requester_user_id=requester_id)
    engine = _mock_engine(ticket=ticket)
    engine.chat_storage = MagicMock()
    intruder = {"user_id": uuid4(), "org_id": uuid4()}
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket_chat(ticket.id, intruder))
    assert exc.value.status_code == 403


def test_get_ticket_chat_ticket_not_found_returns_404():
    engine = _mock_engine(ticket=None)
    engine.chat_storage = MagicMock()
    current_user = {"user_id": uuid4(), "org_id": uuid4()}
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket_chat(uuid4(), current_user))
    assert exc.value.status_code == 404


def test_get_ticket_chat_no_chat_for_ticket_returns_404():
    """Ticket exists but no chat row found (data integrity issue) — return 404."""
    user = _user()
    ticket = _ticket(requester_user_id=user.id)
    engine = _mock_engine(user=user, ticket=ticket)
    engine.chat_storage = MagicMock()
    engine.chat_storage.get_by_support_ticket = AsyncMock(return_value=None)
    current_user = {"user_id": user.id, "org_id": user.org_id}
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket_chat(ticket.id, current_user))
    assert exc.value.status_code == 404


# ---------- GET /support/tickets/{id}/events ----------

def test_get_ticket_events_for_requester_returns_event_dicts():
    user = _user()
    ticket = _ticket(requester_user_id=user.id)
    from src.engine.models.support_ticket_event import (
        SupportTicketEvent, SupportTicketEventType, SupportTicketActorRole,
    )
    events = [
        SupportTicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            actor_role=SupportTicketActorRole.REQUESTER,
            event_type=SupportTicketEventType.CREATED,
        ),
        SupportTicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            actor_role=SupportTicketActorRole.AI,
            event_type=SupportTicketEventType.AI_RESPONDED,
        ),
    ]
    engine = _mock_engine(user=user, ticket=ticket)
    engine.support_ticket_event_storage = MagicMock()
    engine.support_ticket_event_storage.list_by_ticket = AsyncMock(return_value=events)
    current_user = {"user_id": user.id, "org_id": user.org_id}

    with _patch_engine(engine):
        result = _run(get_ticket_events(ticket.id, limit=200, current_user=current_user))

    assert len(result) == 2
    assert result[0]["event_type"] == "created"
    assert result[1]["event_type"] == "ai_responded"
    engine.support_ticket_event_storage.list_by_ticket.assert_called_once_with(ticket.id, 200)


def test_get_ticket_events_unauthorized_returns_403():
    requester_id = uuid4()
    ticket = _ticket(requester_user_id=requester_id)
    engine = _mock_engine(ticket=ticket)
    engine.support_ticket_event_storage = MagicMock()
    intruder = {"user_id": uuid4(), "org_id": uuid4()}
    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_ticket_events(ticket.id, limit=200, current_user=intruder))
    assert exc.value.status_code == 403


def test_get_ticket_events_for_operator_returns_event_dicts():
    operator = _operator_user()
    requester_id = uuid4()
    ticket = _ticket(requester_user_id=requester_id)
    engine = _mock_engine(ticket=ticket)
    engine.support_ticket_event_storage = MagicMock()
    engine.support_ticket_event_storage.list_by_ticket = AsyncMock(return_value=[])
    with _patch_engine(engine):
        result = _run(get_ticket_events(ticket.id, limit=200, current_user=operator))
    assert result == []
