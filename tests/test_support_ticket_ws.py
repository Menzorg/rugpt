import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicket, SupportTicketCategory, SupportTicketStatus, ClosedByRole,
)
from src.engine.services.support_ticket_service import SupportTicketService


def _run(coro):
    return asyncio.run(coro)


def _ticket_update_sends(kafka):
    """All ticket_update payloads published to chat.events."""
    return [
        c.args[1] for c in kafka.send.await_args_list
        if c.args[1].get("kind") == "ticket_update"
    ]


def _svc(*, ticket_storage, chat_storage, kafka):
    return SupportTicketService(
        ticket_storage=ticket_storage, event_storage=AsyncMock(),
        chat_storage=chat_storage, message_storage=AsyncMock(),
        user_storage=AsyncMock(), notification_service=AsyncMock(),
        kafka_producer=kafka,
    )


def _ticket(**kw):
    base = dict(
        requester_user_id=uuid4(), requester_org_id=uuid4(),
        category=SupportTicketCategory.HOW_TO, status=SupportTicketStatus.IN_PROGRESS,
        title="t",
    )
    base.update(kw)
    return SupportTicket(**base)


def test_close_publishes_ticket_update():
    requester_id = uuid4()
    t = _ticket(requester_user_id=requester_id)
    closed = _ticket(requester_user_id=requester_id, status=SupportTicketStatus.CLOSED,
                     closed_by_role=ClosedByRole.REQUESTER)
    ticket_storage = MagicMock()
    ticket_storage.get_by_id = AsyncMock(return_value=t)
    ticket_storage.close_ticket = AsyncMock(return_value=closed)
    chat = MagicMock(id=uuid4())
    chat_storage = MagicMock()
    chat_storage.get_by_support_ticket = AsyncMock(return_value=chat)
    kafka = MagicMock()
    kafka.send = AsyncMock()
    svc = SupportTicketService(
        ticket_storage=ticket_storage, event_storage=AsyncMock(),
        chat_storage=chat_storage, message_storage=AsyncMock(),
        user_storage=AsyncMock(), notification_service=AsyncMock(),
        kafka_producer=kafka,
    )
    by_user = MagicMock(id=requester_id, org_id=t.requester_org_id)
    _run(svc.close_ticket(t.id, by_user))

    # ticket_update published to chat.events for the ticket's chat
    sent = _ticket_update_sends(kafka)
    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(chat.id)
    assert sent[0]["ticket"]["status"] == "closed"


def test_take_publishes_ticket_update():
    operator_id = uuid4()
    taken = _ticket(assignee_user_id=operator_id, status=SupportTicketStatus.IN_PROGRESS)
    ticket_storage = MagicMock()
    ticket_storage.take = AsyncMock(return_value=taken)
    chat = MagicMock(id=uuid4())
    chat_storage = MagicMock()
    chat_storage.get_by_support_ticket = AsyncMock(return_value=chat)
    chat_storage.add_participant = AsyncMock()
    kafka = MagicMock()
    kafka.send = AsyncMock()
    svc = _svc(ticket_storage=ticket_storage, chat_storage=chat_storage, kafka=kafka)
    operator = MagicMock(id=operator_id, org_id=Config.RUGPT_SUPPORT_ORG_ID)
    _run(svc.take_ticket(taken.id, operator))

    sent = _ticket_update_sends(kafka)
    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(chat.id)
    assert sent[0]["ticket"]["status"] == "in_progress"


def test_reopen_publishes_ticket_update():
    closed = _ticket(status=SupportTicketStatus.CLOSED)
    closed.closed_at = datetime.utcnow()
    reopened = _ticket(assignee_user_id=uuid4(), status=SupportTicketStatus.IN_PROGRESS)
    ticket_storage = MagicMock()
    ticket_storage.get_by_id = AsyncMock(return_value=closed)
    ticket_storage.reopen = AsyncMock(return_value=reopened)
    chat = MagicMock(id=uuid4())
    chat_storage = MagicMock()
    chat_storage.get_by_support_ticket = AsyncMock(return_value=chat)
    kafka = MagicMock()
    kafka.send = AsyncMock()
    svc = _svc(ticket_storage=ticket_storage, chat_storage=chat_storage, kafka=kafka)
    _run(svc.reopen_if_within_window(closed.id, by_user=None))

    sent = _ticket_update_sends(kafka)
    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(chat.id)
    assert sent[0]["ticket"]["status"] == "in_progress"


def test_publish_ticket_update_noop_when_chat_missing():
    chat_storage = MagicMock()
    chat_storage.get_by_support_ticket = AsyncMock(return_value=None)
    kafka = MagicMock()
    kafka.send = AsyncMock()
    svc = _svc(ticket_storage=AsyncMock(), chat_storage=chat_storage, kafka=kafka)
    _run(svc._publish_ticket_update(_ticket()))
    kafka.send.assert_not_called()
