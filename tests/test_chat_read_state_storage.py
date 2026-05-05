"""Tests for ChatReadStateStorage.

Project does NOT use pytest-asyncio: synchronous test functions invoke
asyncio.run via the local _run helper.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from src.engine.config import Config
from src.engine.storage.chat_read_state_storage import ChatReadStateStorage


DSN = Config.get_postgres_dsn()


def _run(coro):
    return asyncio.run(coro)


async def _make_storage() -> ChatReadStateStorage:
    storage = ChatReadStateStorage(DSN)
    await storage.init()
    return storage


async def _create_org(storage: ChatReadStateStorage) -> UUID:
    return await storage.fetchval(
        "INSERT INTO organizations (id, name, slug) "
        "VALUES (gen_random_uuid(), 'crs_org', $1) RETURNING id",
        f"crs_{uuid4().hex[:10]}",
    )


async def _create_user(storage: ChatReadStateStorage, org_id: UUID, tag: str) -> UUID:
    return await storage.fetchval(
        "INSERT INTO users (id, org_id, username, name, password_hash, email) "
        "VALUES (gen_random_uuid(), $1, $2, $3, 'x', $4) RETURNING id",
        org_id,
        f"crs_{tag}_{uuid4().hex[:8]}",
        f"CRS {tag}",
        f"crs_{tag}_{uuid4().hex[:8]}@test.local",
    )


async def _create_chat(
    storage: ChatReadStateStorage, org_id: UUID, u1: UUID, u2: UUID,
) -> UUID:
    return await storage.fetchval(
        """
        INSERT INTO chats (id, org_id, type, participants, created_by, is_active)
        VALUES (gen_random_uuid(), $1, 'direct', ARRAY[$2::text, $3::text], $4, true)
        RETURNING id
        """,
        org_id, str(u1), str(u2), u1,
    )


async def _insert_message(
    storage: ChatReadStateStorage,
    chat_id: UUID,
    sender_id: UUID,
    content: str = "hi",
    created_at: datetime = None,
) -> tuple[UUID, datetime]:
    """Insert a message and return (id, created_at)."""
    if created_at is None:
        row = await storage.fetchrow(
            """
            INSERT INTO messages (id, chat_id, sender_type, sender_id, content)
            VALUES (gen_random_uuid(), $1, 'user', $2, $3)
            RETURNING id, created_at
            """,
            chat_id, sender_id, content,
        )
    else:
        row = await storage.fetchrow(
            """
            INSERT INTO messages (id, chat_id, sender_type, sender_id, content, created_at)
            VALUES (gen_random_uuid(), $1, 'user', $2, $3, $4)
            RETURNING id, created_at
            """,
            chat_id, sender_id, content, created_at,
        )
    return row["id"], row["created_at"]


async def _cleanup(storage: ChatReadStateStorage, org_id: UUID) -> None:
    """Hard-delete everything created under this org_id.

    chat_read_state, messages, chats cascade via FK on chats; users cascade
    via FK on org. Order: read_state -> messages -> chats -> users -> org.
    """
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


def test_upsert_creates_row():
    """After upsert with last_read_at = msg.created_at, that chat is no longer unread."""
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            msg_id, msg_ts = await _insert_message(storage, chat_id, u2, "hello u1")

            # Pre-condition: chat has unread for u1
            before = await storage.get_unread_counts_for_user(u1, org_id)
            assert chat_id in before and before[chat_id] == 1

            await storage.upsert(chat_id, u1, msg_id, msg_ts)

            after = await storage.get_unread_counts_for_user(u1, org_id)
            assert chat_id not in after, (
                f"after upsert, chat should not appear in unread map; got {after}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())


def test_upsert_monotonic_guard():
    """Backward upsert must NOT roll the high-water-mark back.

    Setup: msg_old < msg_new. user upserts with msg_new, then tries with msg_old.
    Then we insert msg_between (between old and new). If the guard works, HWM
    is at msg_new and msg_between < HWM => 0 unread. If guard is broken, HWM
    rolled back to msg_old => msg_between counts => 1 unread.
    """
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            now = datetime.now(timezone.utc)
            t_old = now - timedelta(minutes=10)
            t_new = now - timedelta(minutes=2)
            t_between = now - timedelta(minutes=5)

            msg_old, ts_old = await _insert_message(
                storage, chat_id, u2, "old", created_at=t_old,
            )
            msg_new, ts_new = await _insert_message(
                storage, chat_id, u2, "new", created_at=t_new,
            )

            # User reads up to msg_new.
            await storage.upsert(chat_id, u1, msg_new, ts_new)

            # Attempt to roll back to msg_old — guard must reject.
            await storage.upsert(chat_id, u1, msg_old, ts_old)

            # Insert msg_between AFTER both upserts. If HWM is correctly at
            # ts_new, msg_between (< ts_new) is below HWM and not unread.
            await _insert_message(
                storage, chat_id, u2, "between", created_at=t_between,
            )

            counts = await storage.get_unread_counts_for_user(u1, org_id)
            assert chat_id not in counts, (
                f"monotonic guard failed: HWM rolled back to msg_old, "
                f"msg_between is being counted. counts={counts}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())


def test_get_unread_counts_excludes_own():
    """Own messages of u1 must not be counted in u1's unread."""
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            # u1 sends 3 messages, u2 sends 2.
            for _ in range(3):
                await _insert_message(storage, chat_id, u1, "from u1")
            for _ in range(2):
                await _insert_message(storage, chat_id, u2, "from u2")

            counts = await storage.get_unread_counts_for_user(u1, org_id)
            assert counts.get(chat_id) == 2, (
                f"expected 2 (only u2's messages), got {counts.get(chat_id)} "
                f"in {counts}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())


def test_get_unread_counts_excludes_deleted():
    """Soft-deleted messages must not be counted."""
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            msg_id, _ = await _insert_message(storage, chat_id, u2, "doomed")
            await storage.execute(
                "UPDATE messages SET is_deleted = true WHERE id = $1", msg_id,
            )

            counts = await storage.get_unread_counts_for_user(u1, org_id)
            assert chat_id not in counts, (
                f"soft-deleted message must not contribute; counts={counts}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())


def test_get_unread_counts_caps_at_100():
    """Counter caps at 100 even with 120 messages."""
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            # Bulk insert 120 messages from u2 in one statement for speed.
            await storage.execute(
                """
                INSERT INTO messages (id, chat_id, sender_type, sender_id, content)
                SELECT gen_random_uuid(), $1, 'user', $2, 'm'
                FROM generate_series(1, 120)
                """,
                chat_id, u2,
            )

            counts = await storage.get_unread_counts_for_user(u1, org_id)
            assert counts.get(chat_id) == 100, (
                f"expected cap=100, got {counts.get(chat_id)} in {counts}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())


def test_get_unread_counts_skips_zero_chats():
    """Chats with 0 unread must not appear in the result map."""
    async def go():
        storage = await _make_storage()
        org_id = await _create_org(storage)
        try:
            u1 = await _create_user(storage, org_id, "u1")
            u2 = await _create_user(storage, org_id, "u2")
            chat_id = await _create_chat(storage, org_id, u1, u2)

            # Only u1 talks to itself — no foreign messages.
            await _insert_message(storage, chat_id, u1, "monologue")

            counts = await storage.get_unread_counts_for_user(u1, org_id)
            assert chat_id not in counts, (
                f"chat without foreign messages must be omitted; counts={counts}"
            )
        finally:
            await _cleanup(storage, org_id)
            await storage.close()

    _run(go())
