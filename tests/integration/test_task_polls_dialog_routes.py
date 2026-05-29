"""Integration tests for poll dialog routes (Task 11).

Unit-level tests against the route handler functions directly, mirroring the
style of `tests/test_support_routes_client.py`. We mock `get_engine_service`
and pass `current_user` dicts that match the real auth contract
(`user_id` and `org_id` keys, both UUIDs).

Coverage:
  - POST /task-polls/{id}/submit: 400 / 404 / 403 / 409 (status, no msgs)
                                 / 500 (missing chat / interviewer)
                                 / 503 (Kafka fail) / 202 (happy path)
  - GET  /task-polls/today/chat: 200 / 404 (no poll, no chat)
  - GET  /task-polls (list):     include_completed default False filters out
                                 non-pending; True keeps them
"""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.engine.models.message import Message, SenderType
from src.engine.models.task_poll import TaskPoll
from src.engine.models.user import User
from src.engine.routes.task_polls import (
    get_today_chat,
    list_polls,
    submit_poll,
)


_RUGPT_SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")


# ============================================
# Fixtures / helpers
# ============================================


def _user(id_=None, org_id=None, username="x", name="x", email="x@x") -> User:
    return User(
        id=id_ or uuid4(),
        org_id=org_id or uuid4(),
        name=name,
        username=username,
        email=email,
    )


def _poll(
    *,
    id_=None,
    assignee_user_id=None,
    org_id=None,
    status="pending",
    poll_date=None,
) -> TaskPoll:
    return TaskPoll(
        id=id_ or uuid4(),
        org_id=org_id or uuid4(),
        assignee_user_id=assignee_user_id or uuid4(),
        poll_date=poll_date or date.today(),
        status=status,
    )


def _msg(*, chat_id, sender_id, sender_type=SenderType.USER) -> Message:
    return Message(
        chat_id=chat_id,
        sender_type=sender_type,
        sender_id=sender_id,
        content="hello",
    )


def _mock_engine(
    *,
    poll=None,
    chat=None,
    messages=None,
    interviewer=None,
    polls_list=None,
):
    """Build a MagicMock engine with the storages/services poll routes touch."""
    engine = MagicMock()

    engine.task_poll_service = MagicMock()
    engine.task_poll_service.storage = MagicMock()
    engine.task_poll_service.storage.get_by_id = AsyncMock(return_value=poll)
    engine.task_poll_service.storage.get_by_user_and_date = AsyncMock(return_value=poll)
    engine.task_poll_service.list_by_user = AsyncMock(return_value=polls_list or [])

    engine.chat_storage = MagicMock()
    engine.chat_storage.get_by_poll_id = AsyncMock(return_value=chat)

    engine.message_storage = MagicMock()
    # `messages` parameter retained as a hint of "did the assignee write anything?":
    # if any message in the list has sender_id == poll.assignee_user_id, treat as "yes".
    has_user_msg = bool(
        messages
        and poll
        and any(
            getattr(m, "sender_id", None) == poll.assignee_user_id
            for m in messages
        )
    )
    engine.message_storage.messages_exist_from_sender = AsyncMock(
        return_value=has_user_msg,
    )

    engine.user_storage = MagicMock()
    engine.user_storage.get_by_username = AsyncMock(return_value=interviewer)

    engine.ai_service = MagicMock()
    engine.ai_service.enqueue_poll_summary = AsyncMock(return_value=uuid4())

    return engine


def _run(coro):
    return asyncio.run(coro)


def _patch_engine(engine):
    return patch(
        "src.engine.routes.task_polls.get_engine_service",
        return_value=engine,
    )


def _current_user(user_id=None, org_id=None) -> dict:
    return {
        "user_id": user_id or uuid4(),
        "org_id": org_id or uuid4(),
        "is_admin": False,
        "department_id": None,
        "is_head": False,
    }


# ============================================
# submit_poll
# ============================================


def test_submit_invalid_uuid_returns_400():
    engine = _mock_engine()
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll("not-a-uuid", cu))

    assert exc.value.status_code == 400


def test_submit_404_for_missing_poll():
    engine = _mock_engine(poll=None)
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(uuid4()), cu))

    assert exc.value.status_code == 404
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_403_when_not_owner():
    poll = _poll()  # random assignee
    engine = _mock_engine(poll=poll)
    intruder = _current_user()  # random user_id

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), intruder))

    assert exc.value.status_code == 403
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_409_when_already_completed():
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id, status="completed")
    engine = _mock_engine(poll=poll)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 409
    assert "completed" in exc.value.detail
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_409_when_already_expired():
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id, status="expired")
    engine = _mock_engine(poll=poll)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 409
    assert "expired" in exc.value.detail


def test_submit_500_when_chat_missing():
    """Data-integrity: pending poll exists but no chat row attached."""
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    engine = _mock_engine(poll=poll, chat=None)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 500
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_409_when_no_user_messages():
    """Backend guard: assignee never replied to AI in the dialog."""
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    chat = MagicMock(id=uuid4())
    # only AI messages — no user reply
    interviewer_id = uuid4()
    messages = [_msg(chat_id=chat.id, sender_id=interviewer_id, sender_type=SenderType.AI_ROLE)]
    engine = _mock_engine(poll=poll, chat=chat, messages=messages)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 409
    assert "AI" in exc.value.detail
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_500_when_interviewer_missing():
    """Migration 029 didn't run (or row got purged): poll_interviewer_ai missing."""
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    chat = MagicMock(id=uuid4())
    messages = [_msg(chat_id=chat.id, sender_id=user_id)]
    engine = _mock_engine(poll=poll, chat=chat, messages=messages, interviewer=None)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 500
    assert "poll_interviewer_ai" in exc.value.detail
    engine.ai_service.enqueue_poll_summary.assert_not_awaited()


def test_submit_503_when_kafka_publish_fails():
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    chat = MagicMock(id=uuid4())
    messages = [_msg(chat_id=chat.id, sender_id=user_id)]
    interviewer = _user(org_id=_RUGPT_SYSTEM_ORG_ID, username="poll_interviewer_ai")
    engine = _mock_engine(
        poll=poll, chat=chat, messages=messages, interviewer=interviewer,
    )
    engine.ai_service.enqueue_poll_summary = AsyncMock(
        side_effect=RuntimeError("kafka down"),
    )
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(submit_poll(str(poll.id), cu))

    assert exc.value.status_code == 503


def test_submit_returns_202_and_publishes_kafka():
    """User has poll, chat, sent at least one message — submit returns 202
    and calls enqueue_poll_summary with correct args."""
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    chat = MagicMock(id=uuid4())
    messages = [
        _msg(chat_id=chat.id, sender_id=uuid4(), sender_type=SenderType.AI_ROLE),
        _msg(chat_id=chat.id, sender_id=user_id),  # user reply
    ]
    interviewer = _user(org_id=_RUGPT_SYSTEM_ORG_ID, username="poll_interviewer_ai")
    engine = _mock_engine(
        poll=poll, chat=chat, messages=messages, interviewer=interviewer,
    )
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine):
        result = _run(submit_poll(str(poll.id), cu))

    # FastAPI Response object with empty body
    assert result.status_code == 202

    # Verify enqueue called with right args
    call_kwargs = engine.ai_service.enqueue_poll_summary.call_args.kwargs
    assert call_kwargs["poll_id"] == poll.id
    assert call_kwargs["chat_id"] == chat.id
    assert call_kwargs["responder_id"] == interviewer.id

    # Verify poll_interviewer_ai resolved against the system org
    args, kwargs = engine.user_storage.get_by_username.call_args
    assert args[0] == "poll_interviewer_ai"
    assert args[1] == _RUGPT_SYSTEM_ORG_ID


# ============================================
# get_today_chat
# ============================================


def test_today_chat_returns_chat_id_for_active_poll():
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    chat = MagicMock(id=uuid4())
    engine = _mock_engine(poll=poll, chat=chat)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine):
        result = _run(get_today_chat(cu))

    assert result.chat_id == str(chat.id)
    assert result.poll_id == str(poll.id)
    assert result.status == "pending"

    # Storage queried with today's date
    args, _ = engine.task_poll_service.storage.get_by_user_and_date.call_args
    assert args[0] == user_id
    assert args[1] == date.today()


def test_today_chat_404_when_no_poll():
    engine = _mock_engine(poll=None)
    cu = _current_user()

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_today_chat(cu))

    assert exc.value.status_code == 404
    # No chat lookup attempted when poll is missing
    engine.chat_storage.get_by_poll_id.assert_not_awaited()


def test_today_chat_500_when_poll_has_no_chat():
    """Data-integrity: poll exists but its chat is missing — operator alert path
    (distinct from the routine 404 'no poll for today')."""
    user_id = uuid4()
    poll = _poll(assignee_user_id=user_id)
    engine = _mock_engine(poll=poll, chat=None)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine), pytest.raises(HTTPException) as exc:
        _run(get_today_chat(cu))

    assert exc.value.status_code == 500
    assert "data integrity" in exc.value.detail.lower()


# ============================================
# list_polls — include_completed filter
# ============================================


def test_list_polls_default_filters_out_non_pending():
    user_id = uuid4()
    polls = [
        _poll(assignee_user_id=user_id, status="pending"),
        _poll(assignee_user_id=user_id, status="completed"),
        _poll(assignee_user_id=user_id, status="expired"),
        _poll(assignee_user_id=user_id, status="pending"),
    ]
    engine = _mock_engine(polls_list=polls)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine):
        result = _run(list_polls(include_completed=False, limit=30, current_user=cu))

    assert len(result) == 2
    assert all(p.status == "pending" for p in result)


def test_list_polls_include_completed_returns_all():
    user_id = uuid4()
    polls = [
        _poll(assignee_user_id=user_id, status="pending"),
        _poll(assignee_user_id=user_id, status="completed"),
        _poll(assignee_user_id=user_id, status="expired"),
    ]
    engine = _mock_engine(polls_list=polls)
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine):
        result = _run(list_polls(include_completed=True, limit=30, current_user=cu))

    assert len(result) == 3
    assert {p.status for p in result} == {"pending", "completed", "expired"}


def test_list_polls_passes_limit_to_service():
    user_id = uuid4()
    engine = _mock_engine(polls_list=[])
    cu = _current_user(user_id=user_id)

    with _patch_engine(engine):
        _run(list_polls(include_completed=False, limit=7, current_user=cu))

    args, _ = engine.task_poll_service.list_by_user.call_args
    assert args[0] == user_id
    assert args[1] == 7
