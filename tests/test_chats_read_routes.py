"""Route-layer tests for chat read-state endpoints (U4)."""
import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from tests.zt_helpers import override_identity, clear_identity

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def engine_init():
    """Initialize the singleton engine once for this test module."""
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    try:
        await engine.close()
    except Exception:
        pass


@pytest_asyncio.fixture(loop_scope="session")
async def setup(engine_init):
    """Create org + 3 users (u1, u2, outsider) and a fresh asyncpg pool.

    Tests build their own chats/messages on top so each test stays isolated.
    Cleanup wipes chat_read_state, messages, chats, users, organization in FK-safe order.
    """
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'rt-read', $1) RETURNING id",
            f"rt_read_{uuid4().hex[:8]}",
        )
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'U1', 'x', $3) RETURNING id",
            org, f"rt_read_u1_{uuid4().hex[:6]}",
            f"rt_read_u1_{uuid4()}@test.local",
        )
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'U2', 'x', $3) RETURNING id",
            org, f"rt_read_u2_{uuid4().hex[:6]}",
            f"rt_read_u2_{uuid4()}@test.local",
        )
        outsider = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'OUT', 'x', $3) RETURNING id",
            org, f"rt_read_out_{uuid4().hex[:6]}",
            f"rt_read_out_{uuid4()}@test.local",
        )
    yield {
        "engine": engine,
        "pool": pool,
        "org": org,
        "u1": u1,
        "u2": u2,
        "outsider": outsider,
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_read_state WHERE chat_id IN "
            "(SELECT id FROM chats WHERE org_id = $1)", org,
        )
        await conn.execute(
            "DELETE FROM messages WHERE chat_id IN "
            "(SELECT id FROM chats WHERE org_id = $1)", org,
        )
        await conn.execute("DELETE FROM chats WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


async def _create_chat(pool, org_id, participants, created_by):
    """Insert a direct chat with given participants (list[UUID]). Returns chat id."""
    async with pool.acquire() as conn:
        chat_id = await conn.fetchval(
            "INSERT INTO chats (id, org_id, type, participants, created_by, is_active) "
            "VALUES (gen_random_uuid(), $1, 'direct', $2, $3, true) RETURNING id",
            org_id, [str(p) for p in participants], created_by,
        )
    return chat_id


async def _create_message(pool, chat_id, sender_id, content="hi"):
    async with pool.acquire() as conn:
        msg_id = await conn.fetchval(
            "INSERT INTO messages (id, chat_id, sender_type, sender_id, content) "
            "VALUES (gen_random_uuid(), $1, 'user', $2, $3) RETURNING id",
            chat_id, sender_id, content,
        )
    return msg_id


@pytest.mark.asyncio(loop_scope="session")
async def test_post_read_204(setup):
    """Happy path: u1 marks message from u2 as read, returns 204."""
    pool = setup["pool"]
    chat_id = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    msg_id = await _create_message(pool, chat_id, setup["u2"], "hello u1")

    override_identity(app, user_id=setup["u1"], org_id=setup["org"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chats/{chat_id}/read",
                json={"message_id": str(msg_id)},
            )
            assert r.status_code == 204, r.text
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_post_read_404_message_in_other_chat(setup):
    """Message belongs to chat B but request targets chat A → ValueError → 404."""
    pool = setup["pool"]
    chat_a = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    chat_b = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    msg_in_b = await _create_message(pool, chat_b, setup["u2"], "msg in B")

    override_identity(app, user_id=setup["u1"], org_id=setup["org"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chats/{chat_a}/read",
                json={"message_id": str(msg_in_b)},
            )
            assert r.status_code == 404, r.text
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_post_read_403_not_participant(setup):
    """Outsider (not in chat.participants) → PermissionError → 403."""
    pool = setup["pool"]
    chat_id = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    msg_id = await _create_message(pool, chat_id, setup["u2"], "private")

    override_identity(app, user_id=setup["outsider"], org_id=setup["org"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/chats/{chat_id}/read",
                json={"message_id": str(msg_id)},
            )
            assert r.status_code == 403, r.text
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_unread_counts_returns_dict(setup):
    """Chat with 1 foreign message for u1 → response has {chat_id_str: 1}."""
    pool = setup["pool"]
    chat_id = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    await _create_message(pool, chat_id, setup["u2"], "unread foreign")

    override_identity(app, user_id=setup["u1"], org_id=setup["org"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/chats/unread-counts")
            assert r.status_code == 200, r.text
            data = r.json()
            assert isinstance(data, dict)
            assert str(chat_id) in data
            assert data[str(chat_id)] == 1
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_get_unread_counts_only_my_chats(setup):
    """Chats where requesting user is NOT a participant must not appear in result."""
    pool = setup["pool"]
    # Chat where u1 IS participant, with one foreign message
    mine = await _create_chat(pool, setup["org"], [setup["u1"], setup["u2"]], setup["u1"])
    await _create_message(pool, mine, setup["u2"], "for u1")

    # Chat where u1 is NOT participant (only u2 + outsider) with foreign message from u2
    not_mine = await _create_chat(pool, setup["org"], [setup["u2"], setup["outsider"]], setup["u2"])
    await _create_message(pool, not_mine, setup["u2"], "not for u1")

    override_identity(app, user_id=setup["u1"], org_id=setup["org"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/chats/unread-counts")
            assert r.status_code == 200, r.text
            data = r.json()
            assert str(not_mine) not in data
            # Sanity: own chat with foreign message still present
            assert str(mine) in data
    finally:
        clear_identity(app)
