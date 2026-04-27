"""Regression tests: MessageStorage must be cross-org transparent for SUPPORT chats.

Orgship enforcement lives in ChatService.can_user_access_chat (Task 9) and is
applied at the route/service layer. The `messages` table itself has no `org_id`
column (see migration 001) — chat membership is the only data-layer guard
MessageStorage performs (via chat_id). If anyone adds an orgship WHERE clause
or joins to chats.org_id in MessageStorage methods, these tests will fail and
surface the regression.
"""
import asyncio
from uuid import uuid4

import pytest

from src.engine.config import Config
from src.engine.models.chat import Chat, ChatType
from src.engine.models.message import Message, SenderType
from src.engine.storage.chat_storage import ChatStorage
from src.engine.storage.message_storage import MessageStorage


DSN = Config.get_postgres_dsn()
RUGPT_SUPPORT_ORG_ID = Config.RUGPT_SUPPORT_ORG_ID


def _run(coro):
    return asyncio.run(coro)


async def _make_storages():
    chat_s = ChatStorage(DSN)
    msg_s = MessageStorage(DSN)
    await chat_s.init()
    await msg_s.init()
    return chat_s, msg_s


async def _grab_user(storage):
    row = await storage.fetchrow(
        "SELECT id, org_id FROM users WHERE is_system = false LIMIT 1"
    )
    if row is None:
        return None
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
        f"test_op_{uuid4().hex[:8]}",
        "Test Op",
        f"test-op-{uuid4().hex[:8]}@rugpt.support",
    )
    return new_id, True


async def _create_support_chat(chat_storage, requester_id, requester_org_id, op_id):
    # support_ticket_id is FK to support_tickets(id); create a real ticket row.
    ticket_id = await chat_storage.fetchval(
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
        participants=[requester_id, op_id],
        created_by=requester_id,
        support_ticket_id=ticket_id,
    )
    return await chat_storage.create(chat)


async def _cleanup(chat_storage, msg_storage, chat_id, op_id, op_created):
    row = await chat_storage.fetchrow(
        "SELECT support_ticket_id FROM chats WHERE id = $1", chat_id,
    )
    ticket_id = row["support_ticket_id"] if row else None
    await msg_storage.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
    await chat_storage.execute("DELETE FROM chats WHERE id = $1", chat_id)
    if ticket_id is not None:
        await chat_storage.execute(
            "DELETE FROM support_ticket_events WHERE ticket_id = $1", ticket_id,
        )
        await chat_storage.execute(
            "DELETE FROM support_tickets WHERE id = $1", ticket_id,
        )
    if op_created:
        await chat_storage.execute("DELETE FROM users WHERE id = $1", op_id)


def test_list_by_chat_returns_messages_regardless_of_caller_org():
    """list_by_chat is purely chat_id-bound. No orgship filter at storage level.

    If a future change adds `WHERE org_id = $X` (or joins chats.org_id), this
    test fails.
    """
    async def go():
        chat_s, msg_s = await _make_storages()
        try:
            user_data = await _grab_user(chat_s)
            if user_data is None:
                pytest.skip("no non-system user")
            requester_id, requester_org = user_data
            op_id, op_created = await _grab_or_create_operator(chat_s)

            chat = await _create_support_chat(chat_s, requester_id, requester_org, op_id)
            try:
                # Insert a message from the requester into the cross-org chat
                msg = Message(
                    chat_id=chat.id,
                    sender_id=requester_id,
                    sender_type=SenderType.USER,
                    content="hello from cross-org test",
                )
                await msg_s.create(msg)

                # list_by_chat must return the message — no orgship filter
                msgs = await msg_s.list_by_chat(chat.id, limit=10)
                contents = [m.content for m in msgs]
                assert "hello from cross-org test" in contents
            finally:
                await _cleanup(chat_s, msg_s, chat.id, op_id, op_created)
        finally:
            await chat_s.close()
            await msg_s.close()

    _run(go())


def test_create_message_does_not_validate_orgship():
    """create() persists whatever it's given — no orgship cross-check.

    Service/route layer is responsible for ensuring sender membership in the
    chat. Here we insert a message authored by an operator from the RuGPT
    Support org into a chat owned by a *different* org, and the storage must
    accept it without complaint.
    """
    async def go():
        chat_s, msg_s = await _make_storages()
        try:
            user_data = await _grab_user(chat_s)
            if user_data is None:
                pytest.skip("no non-system user")
            requester_id, requester_org = user_data
            assert requester_org != RUGPT_SUPPORT_ORG_ID, "test data setup error"

            op_id, op_created = await _grab_or_create_operator(chat_s)

            chat = await _create_support_chat(chat_s, requester_id, requester_org, op_id)
            try:
                # Operator (different org) writes into requester's chat
                msg = Message(
                    chat_id=chat.id,
                    sender_id=op_id,
                    sender_type=SenderType.USER,
                    content="operator reply",
                )
                created = await msg_s.create(msg)
                assert created.sender_id == op_id
                assert created.chat_id == chat.id

                # list_by_chat surfaces it
                msgs = await msg_s.list_by_chat(chat.id, limit=10)
                assert any(m.content == "operator reply" for m in msgs)
            finally:
                await _cleanup(chat_s, msg_s, chat.id, op_id, op_created)
        finally:
            await chat_s.close()
            await msg_s.close()

    _run(go())
