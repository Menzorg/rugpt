"""Tests for the support-ticket SLA hook.

Since Kafka became mandatory, the AI reply is generated asynchronously by
AgentRequestHandler, which calls AIService.record_support_first_response after
a successful reply. These tests cover that method directly + the handoff
pre-check that still lives in try_auto_respond.

Scenarios:
1. SUPPORT chat with a ticket -> ai_first_response_at stamped + AI_RESPONDED event.
2. how_to + ai_handoff_at SET -> try_auto_respond does NOT enqueue (escalated).
3. Non-support chat -> record_support_first_response is a no-op (no side effects).
4. Support deps not wired -> record_support_first_response degrades silently (no crash).
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.models.chat import Chat, ChatType
from src.engine.models.message import Message, SenderType
from src.engine.models.support_ticket import (
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
)
from src.engine.models.support_ticket_event import (
    SupportTicketActorRole,
    SupportTicketEventType,
)
from src.engine.models.user import User
from src.engine.services.ai_service import AIService


def _user(id_=None, is_system=False) -> User:
    return User(
        id=id_ or uuid4(),
        org_id=uuid4(),
        name="x",
        username="x",
        email="x@x",
        is_system=is_system,
    )


def _chat(type_, participants, support_ticket_id=None) -> Chat:
    return Chat(
        org_id=uuid4(),
        type=type_,
        participants=participants,
        support_ticket_id=support_ticket_id,
    )


def _msg(chat_id, sender_id, content="hi") -> Message:
    return Message(
        id=uuid4(),
        chat_id=chat_id,
        sender_id=sender_id,
        sender_type=SenderType.USER,
        content=content,
    )


def _ticket(category=SupportTicketCategory.HOW_TO, ai_handoff_at=None) -> SupportTicket:
    return SupportTicket(
        id=uuid4(),
        requester_user_id=uuid4(),
        requester_org_id=uuid4(),
        category=category,
        ai_handoff_at=ai_handoff_at,
        status=SupportTicketStatus.OPEN,
    )


def _make_service(
    *,
    chat,
    sender,
    system_responder,
    ticket=None,
    ai_response=None,
):
    """Construct AIService with mocked dependencies. Returns (svc, ticket_storage, event_storage)."""
    chat_storage = MagicMock()
    chat_storage.get_by_id = AsyncMock(return_value=chat)

    user_storage = MagicMock()

    async def _get_user(uid):
        if uid == system_responder.id:
            return system_responder
        if uid == sender.id:
            return sender
        return _user(id_=uid)

    user_storage.get_by_id = AsyncMock(side_effect=_get_user)

    role_storage = MagicMock()
    message_storage = MagicMock()

    # Sync mode: no kafka producer wired
    svc = AIService(
        role_storage=role_storage,
        user_storage=user_storage,
        chat_storage=chat_storage,
        message_storage=message_storage,
        kafka_producer=None,
    )

    # Mock generate_response to return a fake AI reply (or None)
    svc.generate_response = AsyncMock(return_value=ai_response)

    ticket_storage = None
    event_storage = None
    if ticket is not None:
        ticket_storage = MagicMock()
        ticket_storage.get_by_id = AsyncMock(return_value=ticket)
        ticket_storage.set_ai_first_response = AsyncMock()
        event_storage = MagicMock()
        event_storage.insert = AsyncMock()
        svc.support_ticket_storage = ticket_storage
        svc.support_ticket_event_storage = event_storage

    return svc, ticket_storage, event_storage


def _run(coro):
    return asyncio.run(coro)


# ---------- Scenario 1: record_support_first_response stamps + audits ----------

def test_record_support_first_response_stamps_and_audits():
    sender = _user()
    support_ai = _user(is_system=True)
    ticket = _ticket(category=SupportTicketCategory.HOW_TO, ai_handoff_at=None)
    chat = _chat(
        ChatType.SUPPORT,
        participants=[sender.id, support_ai.id],
        support_ticket_id=ticket.id,
    )
    fake_ai_msg = _msg(chat.id, support_ai.id, content="ai reply")

    svc, ticket_storage, event_storage = _make_service(
        chat=chat,
        sender=sender,
        system_responder=support_ai,
        ticket=ticket,
        ai_response=fake_ai_msg,
    )

    _run(svc.record_support_first_response(
        chat_id=chat.id,
        ai_user_id=support_ai.id,
        response_message_id=fake_ai_msg.id,
    ))

    # ai_first_response_at stamped with the chat's ticket id
    ticket_storage.set_ai_first_response.assert_called_once_with(chat.support_ticket_id)
    # AI_RESPONDED event recorded
    event_storage.insert.assert_called_once()
    inserted_event = event_storage.insert.call_args.args[0]
    assert inserted_event.event_type == SupportTicketEventType.AI_RESPONDED
    assert inserted_event.actor_role == SupportTicketActorRole.AI
    assert inserted_event.actor_user_id == support_ai.id
    assert inserted_event.ticket_id == chat.support_ticket_id


# ---------- Scenario 2: handoff already done, AI must not respond ----------

def test_support_chat_after_handoff_ai_does_not_respond():
    sender = _user()
    support_ai = _user(is_system=True)
    ticket = _ticket(
        category=SupportTicketCategory.HOW_TO, ai_handoff_at=datetime.utcnow()
    )
    chat = _chat(
        ChatType.SUPPORT,
        participants=[sender.id, support_ai.id],
        support_ticket_id=ticket.id,
    )
    msg = _msg(chat.id, sender.id)

    svc, ticket_storage, event_storage = _make_service(
        chat=chat,
        sender=sender,
        system_responder=support_ai,
        ticket=ticket,
        ai_response=None,
    )

    result = _run(svc.try_auto_respond(msg, chat.id, sender.id))

    assert result is None
    svc.generate_response.assert_not_called()
    ticket_storage.set_ai_first_response.assert_not_called()
    event_storage.insert.assert_not_called()


# ---------- Scenario 3: non-support chat — record hook is a no-op ----------

def test_record_support_first_response_noop_for_non_support_chat():
    sender = _user()
    system_user = _user(is_system=True)
    chat = _chat(
        ChatType.DIRECT,
        participants=[sender.id, system_user.id],
        support_ticket_id=None,
    )
    fake_ai_msg = _msg(chat.id, system_user.id, content="reply")

    # support deps ARE wired, but the chat is not a SUPPORT chat -> must be a no-op
    svc, ticket_storage, event_storage = _make_service(
        chat=chat,
        sender=sender,
        system_responder=system_user,
        ticket=_ticket(),
        ai_response=fake_ai_msg,
    )

    _run(svc.record_support_first_response(
        chat_id=chat.id,
        ai_user_id=system_user.id,
        response_message_id=fake_ai_msg.id,
    ))

    ticket_storage.set_ai_first_response.assert_not_called()
    event_storage.insert.assert_not_called()


# ---------- Scenario 4: support deps NOT wired — graceful degrade ----------

def test_record_support_first_response_noop_when_support_deps_not_wired():
    """If AIService is constructed without support_ticket_storage/event_storage
    (legacy environments or partial bootstrap), record_support_first_response
    must degrade silently — no crash.
    """
    sender = _user()
    support_ai = _user(is_system=True)
    chat = _chat(
        ChatType.SUPPORT,
        participants=[sender.id, support_ai.id],
        support_ticket_id=uuid4(),
    )
    fake_ai_msg = _msg(chat.id, support_ai.id, content="reply")

    svc, _, _ = _make_service(
        chat=chat,
        sender=sender,
        system_responder=support_ai,
        ticket=None,  # support deps NOT injected
        ai_response=fake_ai_msg,
    )

    # Must not raise.
    _run(svc.record_support_first_response(
        chat_id=chat.id,
        ai_user_id=support_ai.id,
        response_message_id=fake_ai_msg.id,
    ))
