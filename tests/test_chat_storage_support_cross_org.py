"""Regression tests: ChatStorage must be cross-org transparent for SUPPORT chats.

The orgship enforcement lives in ChatService.can_user_access_chat (Task 9).
ChatStorage just exposes data; if anyone adds an orgship WHERE clause to
list_by_user or get_by_id, these tests will fail and surface the regression.
"""
import asyncio
from uuid import uuid4

import pytest

from src.engine.config import Config
from src.engine.models.chat import Chat, ChatType
from src.engine.storage.chat_storage import ChatStorage


DSN = Config.get_postgres_dsn()
RUGPT_SUPPORT_ORG_ID = Config.RUGPT_SUPPORT_ORG_ID


def _run(coro):
    return asyncio.run(coro)


async def _make_storage():
    storage = ChatStorage(DSN)
    await storage.init()
    return storage


async def _grab_user_from_org(storage, where_clause: str = "is_system = false") -> tuple:
    row = await storage.fetchrow(
        f"SELECT id, org_id FROM users WHERE {where_clause} LIMIT 1"
    )
    if row is None:
        return None, None
    return row["id"], row["org_id"]


async def _grab_or_create_operator(storage):
    row = await storage.fetchrow(
        "SELECT id FROM users WHERE org_id = $1 AND is_active = true LIMIT 1",
        RUGPT_SUPPORT_ORG_ID,
    )
    if row:
        return row["id"], False
    new_id = await storage.fetchval(
        """
        INSERT INTO users (org_id, username, name, email, is_active)
        VALUES ($1, $2, $3, $4, true) RETURNING id
        """,
        RUGPT_SUPPORT_ORG_ID,
        f"test_operator_{uuid4().hex[:8]}",
        "Test Operator",
        f"test-op-{uuid4().hex[:8]}@rugpt.support",
    )
    return new_id, True


async def _create_support_chat(storage, requester_id, requester_org_id, operator_id):
    # chats.support_ticket_id is a FK to support_tickets(id); create a real
    # ticket row first so the chat can reference it.
    ticket_id = await storage.fetchval(
        """
        INSERT INTO support_tickets (
            requester_user_id, requester_org_id, category, status
        )
        VALUES ($1, $2, 'how_to', 'open')
        RETURNING id
        """,
        requester_id, requester_org_id,
    )
    chat = Chat(
        org_id=requester_org_id,
        type=ChatType.SUPPORT,
        participants=[requester_id, operator_id],
        created_by=requester_id,
        support_ticket_id=ticket_id,
    )
    return await storage.create(chat)


async def _cleanup_chat(storage, chat_id):
    # Capture ticket_id before chats row goes away so we can clean it up too.
    row = await storage.fetchrow(
        "SELECT support_ticket_id FROM chats WHERE id = $1", chat_id,
    )
    ticket_id = row["support_ticket_id"] if row else None
    await storage.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
    await storage.execute("DELETE FROM chats WHERE id = $1", chat_id)
    if ticket_id is not None:
        await storage.execute(
            "DELETE FROM support_ticket_events WHERE ticket_id = $1", ticket_id,
        )
        await storage.execute(
            "DELETE FROM support_tickets WHERE id = $1", ticket_id,
        )


def test_get_by_id_returns_chat_regardless_of_caller_org():
    """get_by_id is data-layer only — no orgship check at storage level.

    If a future change adds `WHERE org_id = $X` to get_by_id, this test fails.
    """
    async def go():
        storage = await _make_storage()
        try:
            requester_id, requester_org = await _grab_user_from_org(storage)
            if requester_id is None:
                pytest.skip("no non-system user")
            op_id, op_created = await _grab_or_create_operator(storage)

            chat = await _create_support_chat(storage, requester_id, requester_org, op_id)
            try:
                # Storage exposes the chat regardless of caller org —
                # access control happens at service layer.
                fetched = await storage.get_by_id(chat.id)
                assert fetched is not None
                assert fetched.type == ChatType.SUPPORT
                assert fetched.org_id == requester_org
                assert fetched.support_ticket_id == chat.support_ticket_id
            finally:
                await _cleanup_chat(storage, chat.id)
                if op_created:
                    await storage.execute("DELETE FROM users WHERE id = $1", op_id)
        finally:
            await storage.close()

    _run(go())


def test_list_by_user_returns_support_chat_for_operator_from_foreign_org():
    """Operator from RuGPT Support org IS a participant of the support chat;
    list_by_user must return that chat even though chat.org_id != operator.org_id.
    """
    async def go():
        storage = await _make_storage()
        try:
            requester_id, requester_org = await _grab_user_from_org(storage)
            if requester_id is None:
                pytest.skip("no non-system user")
            assert requester_org != RUGPT_SUPPORT_ORG_ID, "test data setup error"

            op_id, op_created = await _grab_or_create_operator(storage)
            chat = await _create_support_chat(storage, requester_id, requester_org, op_id)
            try:
                # Operator's chat list must include the cross-org support chat
                op_chats = await storage.list_by_user(op_id)
                op_chat_ids = {c.id for c in op_chats}
                assert chat.id in op_chat_ids, (
                    "Cross-org SUPPORT chat must be visible to operator via list_by_user "
                    "(orgship is enforced at service layer, not storage)."
                )
            finally:
                await _cleanup_chat(storage, chat.id)
                if op_created:
                    await storage.execute("DELETE FROM users WHERE id = $1", op_id)
        finally:
            await storage.close()

    _run(go())


def test_list_by_user_does_NOT_return_chat_to_non_participant():
    """Storage layer guard: list_by_user filters by participants array.

    A user who is NOT in chat.participants must not see the chat, even if
    they are in the same org as the chat. This protects against accidental
    leak via chat-list endpoints when service-layer auth is bypassed.
    """
    async def go():
        storage = await _make_storage()
        try:
            requester_id, requester_org = await _grab_user_from_org(storage)
            if requester_id is None:
                pytest.skip("no non-system user")
            # Find a different user in the SAME org (not in chat participants)
            other_row = await storage.fetchrow(
                """
                SELECT id FROM users
                WHERE org_id = $1 AND is_system = false AND id != $2
                LIMIT 1
                """,
                requester_org, requester_id,
            )
            if other_row is None:
                pytest.skip("no second non-system user in requester org")
            other_id = other_row["id"]

            op_id, op_created = await _grab_or_create_operator(storage)
            chat = await _create_support_chat(storage, requester_id, requester_org, op_id)
            try:
                other_chats = await storage.list_by_user(other_id)
                other_chat_ids = {c.id for c in other_chats}
                assert chat.id not in other_chat_ids, (
                    "Storage participant filter failure: user not in participants "
                    "should not see the chat."
                )
            finally:
                await _cleanup_chat(storage, chat.id)
                if op_created:
                    await storage.execute("DELETE FROM users WHERE id = $1", op_id)
        finally:
            await storage.close()

    _run(go())
