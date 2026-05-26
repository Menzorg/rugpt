import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.models.support_ticket import SupportTicketCategory
from src.engine.services.support_ticket_service import SupportTicketService


def _run(coro):
    return asyncio.run(coro)


def _make_service():
    ticket_storage = MagicMock()
    ticket_storage.create = AsyncMock(side_effect=lambda t: t)
    event_storage = MagicMock()
    event_storage.insert = AsyncMock()
    chat_storage = MagicMock()
    chat_storage.create = AsyncMock(side_effect=lambda c: c)
    chat_storage.update_last_message = AsyncMock()
    message_storage = MagicMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)
    user_storage = MagicMock()
    support_ai = MagicMock(id=uuid4(), is_system=True)
    user_storage.get_by_username = AsyncMock(return_value=support_ai)
    notification_service = AsyncMock()
    ai_service = MagicMock()
    ai_service.try_auto_respond = AsyncMock(return_value=None)

    svc = SupportTicketService(
        ticket_storage=ticket_storage,
        event_storage=event_storage,
        chat_storage=chat_storage,
        message_storage=message_storage,
        user_storage=user_storage,
        notification_service=notification_service,
        ai_service=ai_service,
    )
    return svc, ai_service, notification_service


def _requester():
    # MagicMock avoids coupling the test to the exact User constructor signature.
    return MagicMock(id=uuid4(), org_id=uuid4())


def test_how_to_triggers_ai_auto_respond():
    svc, ai_service, notif = _make_service()
    requester = _requester()
    _run(svc.create_ticket(requester, SupportTicketCategory.HOW_TO, "как сделать X?"))
    ai_service.try_auto_respond.assert_awaited_once()
    notif.notify_new_in_queue.assert_not_awaited()
    # AI is triggered on the persisted first message, in the ticket's chat,
    # on behalf of the requester.
    call = ai_service.try_auto_respond.await_args
    first_message = svc.message_storage.create.await_args.args[0]
    chat = svc.chat_storage.create.await_args.args[0]
    assert call.args == (first_message, chat.id, requester.id)


def test_bug_notifies_queue_no_ai():
    svc, ai_service, notif = _make_service()
    _run(svc.create_ticket(_requester(), SupportTicketCategory.BUG, "падает Y"))
    ai_service.try_auto_respond.assert_not_awaited()
    notif.notify_new_in_queue.assert_awaited_once()


def test_how_to_without_ai_service_is_noop():
    # Graceful degradation: how_to with no ai_service wired must not raise
    # and must not fall through to notifying the operator queue.
    svc, _ai, notif = _make_service()
    svc.ai_service = None
    _run(svc.create_ticket(_requester(), SupportTicketCategory.HOW_TO, "вопрос"))
    notif.notify_new_in_queue.assert_not_awaited()
