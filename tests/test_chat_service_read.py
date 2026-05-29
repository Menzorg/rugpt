"""Tests for ChatService.mark_chat_read and ChatService.list_unread_counts.

Project does NOT use pytest-asyncio: synchronous test functions invoke
asyncio.run via the local _run helper.
"""
import asyncio
from uuid import UUID, uuid4

import pytest

from src.engine.config import Config
from src.engine.services.chat_service import ChatService
from src.engine.storage.chat_read_state_storage import ChatReadStateStorage
from src.engine.storage.chat_storage import ChatStorage
from src.engine.storage.message_storage import MessageStorage


DSN = Config.get_postgres_dsn()


def _run(coro):
    return asyncio.run(coro)


async def _bootstrap():
    """Init the storages we need and compose a ChatService.

    Other ChatService deps (user_file_storage, message_attachment_storage)
    are left None — these tests only exercise mark_chat_read /
    list_unread_counts, which never touch them.
    """
    chat_storage = ChatStorage(DSN)
    message_storage = MessageStorage(DSN)
    chat_read_state_storage = ChatReadStateStorage(DSN)
    await chat_storage.init()
    await message_storage.init()
    await chat_read_state_storage.init()

    service = ChatService(
        chat_storage=chat_storage,
        message_storage=message_storage,
        chat_read_state_storage=chat_read_state_storage,
    )
    return service, chat_storage, message_storage, chat_read_state_storage


async def _close_all(*storages):
    for s in storages:
        await s.close()


async def _create_org(storage) -> UUID:
    return await storage.fetchval(
        "INSERT INTO organizations (id, name, slug) "
        "VALUES (gen_random_uuid(), 'csr_org', $1) RETURNING id",
        f"csr_{uuid4().hex[:10]}",
    )


async def _create_user(storage, org_id: UUID, tag: str) -> UUID:
    return await storage.fetchval(
        "INSERT INTO users (id, org_id, username, name, password_hash, email) "
        "VALUES (gen_random_uuid(), $1, $2, $3, 'x', $4) RETURNING id",
        org_id,
        f"csr_{tag}_{uuid4().hex[:8]}",
        f"CSR {tag}",
        f"csr_{tag}_{uuid4().hex[:8]}@test.local",
    )


async def _create_chat(storage, org_id: UUID, participants) -> UUID:
    return await storage.fetchval(
        """
        INSERT INTO chats (id, org_id, type, participants, created_by, is_active)
        VALUES (gen_random_uuid(), $1, 'direct', $2, $3, true)
        RETURNING id
        """,
        org_id,
        [str(p) for p in participants],
        participants[0],
    )


async def _insert_message(storage, chat_id: UUID, sender_id: UUID, content: str = "hi") -> UUID:
    row = await storage.fetchrow(
        """
        INSERT INTO messages (id, chat_id, sender_type, sender_id, content)
        VALUES (gen_random_uuid(), $1, 'user', $2, $3)
        RETURNING id, created_at
        """,
        chat_id, sender_id, content,
    )
    return row["id"]


async def _cleanup(storage, org_id: UUID) -> None:
    """Hard-delete everything created under this org_id."""
    await storage.execute(
        "DELETE FROM chat_read_state WHERE chat_id IN "
        "(SELECT id FROM chats WHERE org_id = $1)",
        org_id,
    )
    await storage.execute(
        "DELETE FROM messages WHERE chat_id IN "
        "(SELECT id FROM chats WHERE org_id = $1)",
        org_id,
    )
    await storage.execute("DELETE FROM chats WHERE org_id = $1", org_id)
    await storage.execute("DELETE FROM users WHERE org_id = $1", org_id)
    await storage.execute("DELETE FROM organizations WHERE id = $1", org_id)


def test_mark_chat_read_happy_path():
    """After mark_chat_read with the latest message, that chat disappears
    from the user's unread map (counter == 0)."""
    async def go():
        service, chat_storage, message_storage, crs = await _bootstrap()
        org_id = await _create_org(chat_storage)
        try:
            u1 = await _create_user(chat_storage, org_id, "u1")
            u2 = await _create_user(chat_storage, org_id, "u2")
            chat_id = await _create_chat(chat_storage, org_id, [u1, u2])
            msg_id = await _insert_message(chat_storage, chat_id, u2, "hello u1")

            # Pre-condition: u1 has 1 unread in this chat.
            before = await crs.get_unread_counts_for_user(u1, org_id)
            assert before.get(chat_id) == 1

            await service.mark_chat_read(
                chat_id=chat_id, user_id=u1, message_id=msg_id,
            )

            after = await crs.get_unread_counts_for_user(u1, org_id)
            assert chat_id not in after, (
                f"after mark_chat_read, chat must vanish from unread map; got {after}"
            )
        finally:
            await _cleanup(chat_storage, org_id)
            await _close_all(chat_storage, message_storage, crs)

    _run(go())


def test_mark_chat_read_raises_when_message_not_in_chat():
    """If the message belongs to a different chat, mark_chat_read raises ValueError."""
    async def go():
        service, chat_storage, message_storage, crs = await _bootstrap()
        org_id = await _create_org(chat_storage)
        try:
            u1 = await _create_user(chat_storage, org_id, "u1")
            u2 = await _create_user(chat_storage, org_id, "u2")
            chat_a = await _create_chat(chat_storage, org_id, [u1, u2])
            chat_b = await _create_chat(chat_storage, org_id, [u1, u2])
            msg_in_b = await _insert_message(chat_storage, chat_b, u2, "in b")

            with pytest.raises(ValueError, match="not in chat"):
                await service.mark_chat_read(
                    chat_id=chat_a, user_id=u1, message_id=msg_in_b,
                )
        finally:
            await _cleanup(chat_storage, org_id)
            await _close_all(chat_storage, message_storage, crs)

    _run(go())


def test_mark_chat_read_raises_when_not_participant():
    """If user is not a participant of the chat, mark_chat_read raises PermissionError."""
    async def go():
        service, chat_storage, message_storage, crs = await _bootstrap()
        org_id = await _create_org(chat_storage)
        try:
            u1 = await _create_user(chat_storage, org_id, "u1")
            u2 = await _create_user(chat_storage, org_id, "u2")
            u3 = await _create_user(chat_storage, org_id, "u3")
            chat_id = await _create_chat(chat_storage, org_id, [u1, u2])
            msg_id = await _insert_message(chat_storage, chat_id, u2, "hi")

            with pytest.raises(PermissionError, match="participant"):
                await service.mark_chat_read(
                    chat_id=chat_id, user_id=u3, message_id=msg_id,
                )
        finally:
            await _cleanup(chat_storage, org_id)
            await _close_all(chat_storage, message_storage, crs)

    _run(go())


def test_list_unread_counts_returns_dict():
    """list_unread_counts returns dict[UUID,int] with the right count when no
    read-state has been recorded yet."""
    async def go():
        service, chat_storage, message_storage, crs = await _bootstrap()
        org_id = await _create_org(chat_storage)
        try:
            u1 = await _create_user(chat_storage, org_id, "u1")
            u2 = await _create_user(chat_storage, org_id, "u2")
            chat_id = await _create_chat(chat_storage, org_id, [u1, u2])
            await _insert_message(chat_storage, chat_id, u2, "one")

            result = await service.list_unread_counts(u1, org_id)
            assert isinstance(result, dict)
            assert result.get(chat_id) == 1, (
                f"expected 1 unread for chat {chat_id}; got {result}"
            )
        finally:
            await _cleanup(chat_storage, org_id)
            await _close_all(chat_storage, message_storage, crs)

    _run(go())
