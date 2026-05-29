"""Tests for SupportTicketService — business logic on top of storages.

Uses real Postgres for state transitions; mocks notification service for
side-effect verification.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.engine.config import Config
from src.engine.models.support_ticket import (
    SupportTicketCategory,
    SupportTicketStatus,
    ClosedByRole,
)
from src.engine.models.support_ticket_event import (
    SupportTicketEventType,
    SupportTicketActorRole,
)
from src.engine.models.user import User
from src.engine.services.support_ticket_service import SupportTicketService
from src.engine.storage.chat_storage import ChatStorage
from src.engine.storage.message_storage import MessageStorage
from src.engine.storage.support_ticket_storage import SupportTicketStorage
from src.engine.storage.support_ticket_event_storage import SupportTicketEventStorage
from src.engine.storage.user_storage import UserStorage


DSN = Config.get_postgres_dsn()
SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")


def _run(coro):
    return asyncio.run(coro)


async def _make_service(notif_mock=None):
    chat_storage = ChatStorage(DSN)
    msg_storage = MessageStorage(DSN)
    user_storage = UserStorage(DSN)
    ticket_storage = SupportTicketStorage(DSN)
    event_storage = SupportTicketEventStorage(DSN)
    for s in (chat_storage, msg_storage, user_storage, ticket_storage, event_storage):
        await s.init()

    notification = notif_mock or AsyncMock()
    svc = SupportTicketService(
        ticket_storage=ticket_storage,
        event_storage=event_storage,
        chat_storage=chat_storage,
        message_storage=msg_storage,
        user_storage=user_storage,
        notification_service=notification,
    )
    return svc, chat_storage, msg_storage, user_storage, ticket_storage, event_storage


async def _close_storages(*storages):
    for s in storages:
        await s.close()


async def _grab_user(storage):
    """Grab any non-system user from a non-Support org."""
    row = await storage.fetchrow(
        """
        SELECT id, org_id FROM users
        WHERE is_system = false
          AND org_id != $1
          AND org_id != $2
        LIMIT 1
        """,
        Config.RUGPT_SUPPORT_ORG_ID,
        SYSTEM_ORG_ID,
    )
    if row is None:
        return None
    return User(
        id=row["id"], org_id=row["org_id"],
        name="Test", username="test", email="t@t",
    )


async def _grab_or_create_operator(user_storage):
    row = await user_storage.fetchrow(
        "SELECT id FROM users WHERE org_id = $1 AND is_active = true LIMIT 1",
        Config.RUGPT_SUPPORT_ORG_ID,
    )
    if row:
        return (
            User(
                id=row["id"], org_id=Config.RUGPT_SUPPORT_ORG_ID,
                name="Op", username="op", email="op@t",
            ),
            False,
        )
    new_id = await user_storage.fetchval(
        """
        INSERT INTO users (org_id, username, name, email, is_active)
        VALUES ($1, $2, $3, $4, true) RETURNING id
        """,
        Config.RUGPT_SUPPORT_ORG_ID,
        f"test_op_{uuid4().hex[:8]}",
        "Test Op",
        f"test-op-{uuid4().hex[:8]}@rugpt.support",
    )
    return (
        User(
            id=new_id, org_id=Config.RUGPT_SUPPORT_ORG_ID,
            name="Op", username="op", email="op@t",
        ),
        True,
    )


async def _cleanup(ticket_storage, ticket_id):
    await ticket_storage.execute(
        "DELETE FROM support_ticket_events WHERE ticket_id = $1", ticket_id
    )
    await ticket_storage.execute(
        "DELETE FROM messages WHERE chat_id IN ("
        "SELECT id FROM chats WHERE support_ticket_id = $1)",
        ticket_id,
    )
    await ticket_storage.execute(
        "DELETE FROM chats WHERE support_ticket_id = $1", ticket_id
    )
    await ticket_storage.execute(
        "DELETE FROM support_tickets WHERE id = $1", ticket_id
    )


# ---------- create_ticket ----------

def test_create_how_to_adds_support_ai_to_participants_and_does_not_handoff():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user,
                category=SupportTicketCategory.HOW_TO,
                initial_message="как создать чат?",
            )
            try:
                assert ticket.category == SupportTicketCategory.HOW_TO
                assert ticket.status == SupportTicketStatus.OPEN
                # AI is engaged for how_to → no handoff yet
                assert ticket.ai_handoff_at is None
                # support_ai is in chat.participants
                support_ai = await storages[2].get_by_username(
                    Config.SUPPORT_AI_USERNAME, SYSTEM_ORG_ID,
                )
                assert support_ai is not None
                assert support_ai.id in chat.participants
                assert user.id in chat.participants
                # Chat type and org binding
                assert chat.type.value == "support"
                assert chat.org_id == user.org_id
                assert chat.support_ticket_id == ticket.id
                # First message saved
                msgs = await storages[1].list_by_chat(chat.id, limit=10)
                assert len(msgs) >= 1
                assert any("создать чат" in m.content for m in msgs)
                # Audit event 'created'
                events = await storages[4].list_by_ticket(ticket.id)
                assert any(
                    e.event_type == SupportTicketEventType.CREATED for e in events
                )
                # Notification: how_to → no fan-out (AI is handling)
                assert not notif.notify_new_in_queue.called
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_create_bug_sets_handoff_immediately_and_notifies_queue():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user,
                category=SupportTicketCategory.BUG,
                initial_message="не открывается страница",
            )
            try:
                assert ticket.category == SupportTicketCategory.BUG
                assert ticket.ai_handoff_at is not None
                # support_ai NOT in participants for non-how_to
                support_ai = await storages[2].get_by_username(
                    Config.SUPPORT_AI_USERNAME, SYSTEM_ORG_ID,
                )
                assert support_ai.id not in chat.participants
                # Operator queue notified
                notif.notify_new_in_queue.assert_called_once()
                args = notif.notify_new_in_queue.call_args
                # Either (ticket,) or (ticket=ticket); accept either
                called_with = args.args[0] if args.args else args.kwargs.get("ticket")
                assert called_with.id == ticket.id
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_create_other_category_handles_like_bug():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user,
                category=SupportTicketCategory.OTHER,
                initial_message="есть предложение",
            )
            try:
                assert ticket.ai_handoff_at is not None
                notif.notify_new_in_queue.assert_called_once()
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


# ---------- escalate ----------

def test_escalate_sets_handoff_and_notifies_queue():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user,
                category=SupportTicketCategory.HOW_TO,
                initial_message="вопрос",
            )
            try:
                notif.notify_new_in_queue.reset_mock()
                updated = await svc.escalate(ticket.id, by_user=user)
                assert updated.ai_handoff_at is not None
                # Audit event
                events = await storages[4].list_by_ticket(ticket.id)
                assert any(
                    e.event_type == SupportTicketEventType.AI_HANDOFF for e in events
                )
                # Queue notified once now (escalate moves it to operator queue)
                notif.notify_new_in_queue.assert_called_once()
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_escalate_only_by_requester():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="вопрос",
            )
            try:
                stranger = User(
                    id=uuid4(), org_id=uuid4(),
                    name="x", username="x", email="x@x",
                )
                with pytest.raises(PermissionError):
                    await svc.escalate(ticket.id, by_user=stranger)
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


# ---------- take_ticket ----------

def test_take_by_operator_assigns_and_adds_to_participants_and_notifies():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        chat_storage, _, user_storage, ticket_storage, _ = storages
        try:
            user = await _grab_user(user_storage)
            if user is None:
                pytest.skip("no non-system user")
            operator, op_created = await _grab_or_create_operator(user_storage)
            ticket, chat = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.BUG,
                initial_message="баг",
            )
            try:
                taken = await svc.take_ticket(ticket.id, operator)
                assert taken.status == SupportTicketStatus.IN_PROGRESS
                assert taken.assignee_user_id == operator.id
                # Operator added to chat.participants
                refreshed = await chat_storage.get_by_id(chat.id)
                assert operator.id in refreshed.participants
                # Audit
                events = await storages[4].list_by_ticket(ticket.id)
                assert any(
                    e.event_type == SupportTicketEventType.TAKEN for e in events
                )
                # Client notified
                notif.notify_taken.assert_called_once()
            finally:
                await _cleanup(ticket_storage, ticket.id)
                if op_created:
                    await user_storage.execute(
                        "DELETE FROM users WHERE id = $1", operator.id
                    )
        finally:
            await _close_storages(*storages)

    _run(go())


def test_take_rejects_non_operator():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.BUG,
                initial_message="баг",
            )
            try:
                with pytest.raises(PermissionError):
                    # requester is not operator
                    await svc.take_ticket(ticket.id, user)
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


# ---------- close_ticket ----------

def test_close_by_requester_records_role():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                closed = await svc.close_ticket(ticket.id, by_user=user)
                assert closed.status == SupportTicketStatus.CLOSED
                assert closed.closed_by_role == ClosedByRole.REQUESTER
                events = await storages[4].list_by_ticket(ticket.id)
                assert any(
                    e.event_type == SupportTicketEventType.CLOSED for e in events
                )
                notif.notify_closed.assert_called_once()
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_close_by_operator_records_role():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        user_storage, ticket_storage = storages[2], storages[3]
        try:
            user = await _grab_user(user_storage)
            if user is None:
                pytest.skip("no non-system user")
            operator, op_created = await _grab_or_create_operator(user_storage)
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.BUG,
                initial_message="bug",
            )
            try:
                await svc.take_ticket(ticket.id, operator)
                closed = await svc.close_ticket(ticket.id, by_user=operator)
                assert closed.closed_by_role == ClosedByRole.OPERATOR
            finally:
                await _cleanup(ticket_storage, ticket.id)
                if op_created:
                    await user_storage.execute(
                        "DELETE FROM users WHERE id = $1", operator.id
                    )
        finally:
            await _close_storages(*storages)

    _run(go())


def test_close_by_unrelated_user_forbidden():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                stranger = User(
                    id=uuid4(), org_id=uuid4(),
                    name="x", username="x", email="x@x",
                )
                with pytest.raises(PermissionError):
                    await svc.close_ticket(ticket.id, by_user=stranger)
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


# ---------- reopen_if_within_window ----------

def test_reopen_within_window_succeeds():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                # closed_at is now() — within window for default 7 days
                reopened = await svc.reopen_if_within_window(ticket.id)
                assert reopened is not None
                # Ticket was never taken (no assignee) → reopen returns it to
                # the queue as OPEN, not IN_PROGRESS (status routes by assignee).
                assert reopened.status == SupportTicketStatus.OPEN
                events = await storages[4].list_by_ticket(ticket.id)
                assert any(
                    e.event_type == SupportTicketEventType.REOPENED for e in events
                )
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_reopen_outside_window_returns_none():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                # Force closed_at to be far in the past
                window = Config.SUPPORT_REOPEN_WINDOW_DAYS
                old = datetime.utcnow() - timedelta(days=window + 1)
                await ticket_storage.execute(
                    "UPDATE support_tickets SET closed_at = $1 WHERE id = $2",
                    old, ticket.id,
                )
                result = await svc.reopen_if_within_window(ticket.id)
                assert result is None
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_reopen_with_requester_as_by_user_attributes_event_correctly():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                reopened = await svc.reopen_if_within_window(ticket.id, by_user=user)
                assert reopened is not None
                events = await storages[4].list_by_ticket(ticket.id)
                reopen_events = [e for e in events if e.event_type == SupportTicketEventType.REOPENED]
                assert len(reopen_events) == 1
                assert reopen_events[0].actor_role == SupportTicketActorRole.REQUESTER
                assert reopen_events[0].actor_user_id == user.id
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_escalate_on_closed_ticket_raises():
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, _ = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                with pytest.raises(ValueError, match="closed"):
                    await svc.escalate(ticket.id, by_user=user)
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


# ---------- handle_incoming_message (route hook) ----------

def test_handle_incoming_message_reopens_closed_in_window():
    """SUPPORT chat with closed-in-window ticket → reopen, no exception."""
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                # closed_at is now() — within window
                # Should NOT raise; should call reopen
                await svc.handle_incoming_message(chat, user.id)
                # Ticket was never taken (no assignee) → reopen routes it back
                # to the queue as OPEN (status routes by assignee).
                refreshed = await ticket_storage.get_by_id(ticket.id)
                assert refreshed.status == SupportTicketStatus.OPEN
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_handle_incoming_message_archived_ticket_raises_403():
    """SUPPORT chat with closed-out-of-window ticket → HTTPException 403."""
    async def go():
        from datetime import datetime, timedelta
        from fastapi import HTTPException

        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                await svc.close_ticket(ticket.id, by_user=user)
                # Force closed_at to be far in the past
                window = Config.SUPPORT_REOPEN_WINDOW_DAYS
                old = datetime.utcnow() - timedelta(days=window + 1)
                await ticket_storage.execute(
                    "UPDATE support_tickets SET closed_at = $1 WHERE id = $2",
                    old, ticket.id,
                )
                with pytest.raises(HTTPException) as exc:
                    await svc.handle_incoming_message(chat, user.id)
                assert exc.value.status_code == 403
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_handle_incoming_message_open_ticket_is_noop():
    """SUPPORT chat with OPEN/IN_PROGRESS ticket → no-op (no exception, no reopen call)."""
    async def go():
        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        ticket_storage = storages[3]
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            ticket, chat = await svc.create_ticket(
                requester=user, category=SupportTicketCategory.HOW_TO,
                initial_message="q",
            )
            try:
                # Ticket is OPEN — handle_incoming_message must be a no-op
                await svc.handle_incoming_message(chat, user.id)
                refreshed = await ticket_storage.get_by_id(ticket.id)
                assert refreshed.status == SupportTicketStatus.OPEN
            finally:
                await _cleanup(ticket_storage, ticket.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_handle_incoming_message_non_support_chat_is_noop():
    """DIRECT/TASK/PROJECT chat → no-op (gated on chat.type == SUPPORT)."""
    async def go():
        from src.engine.models.chat import Chat, ChatType
        from uuid import uuid4

        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            # Synthetic non-support chat — should be ignored
            non_support_chat = Chat(
                id=uuid4(),
                org_id=user.org_id,
                type=ChatType.DIRECT,
                participants=[user.id],
                support_ticket_id=None,
            )
            # No exception, no DB activity (no ticket lookup since type != SUPPORT)
            await svc.handle_incoming_message(non_support_chat, user.id)
        finally:
            await _close_storages(*storages)

    _run(go())


def test_handle_incoming_message_support_chat_with_no_ticket_id_is_noop():
    """SUPPORT chat without support_ticket_id (defensive / pathological) → no-op."""
    async def go():
        from src.engine.models.chat import Chat, ChatType
        from uuid import uuid4

        notif = AsyncMock()
        svc, *storages = await _make_service(notif)
        try:
            user = await _grab_user(storages[0])
            if user is None:
                pytest.skip("no non-system user")
            chat_no_ticket = Chat(
                id=uuid4(),
                org_id=user.org_id,
                type=ChatType.SUPPORT,
                participants=[user.id],
                support_ticket_id=None,  # pathological — no linked ticket
            )
            # Should silently no-op, not crash
            await svc.handle_incoming_message(chat_no_ticket, user.id)
        finally:
            await _close_storages(*storages)

    _run(go())
