"""Plan B Task 6: identity migration for routes/notifications.py and
routes/in_app_notifications.py.

Proves the handlers derive the actor (user_id/org_id) from current_user
(device-signed identity) and no longer accept actor params in the request
body / query:

- POST /notifications/channels with NO actor user_id/org_id in the body →
  the channel row is created for the current user (org_id stamped from
  current_user, not from the client).
- GET /in-app-notifications and /in-app-notifications/unread-count with NO
  actor params → only the current user's notifications come back (two users
  seeded; isolation asserted both directions).

Real Postgres rows + override_identity (loop_scope="session").
"""
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
    """Create org + 2 users (u1, u2) and a fresh asyncpg pool.

    Cleanup wipes notification_channels / notification_log / in_app_notifications
    and the users + organization in FK-safe order.
    """
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'rt-notif', $1) RETURNING id",
            f"rt_notif_{uuid4().hex[:8]}",
        )
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'U1', 'x', $3) RETURNING id",
            org, f"rt_notif_u1_{uuid4().hex[:6]}",
            f"rt_notif_u1_{uuid4()}@test.local",
        )
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'U2', 'x', $3) RETURNING id",
            org, f"rt_notif_u2_{uuid4().hex[:6]}",
            f"rt_notif_u2_{uuid4()}@test.local",
        )
    yield {"engine": engine, "pool": pool, "org": org, "u1": u1, "u2": u2}
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM in_app_notifications WHERE org_id = $1", org)
        await conn.execute(
            "DELETE FROM notification_log WHERE user_id IN ($1, $2)", u1, u2)
        await conn.execute(
            "DELETE FROM notification_channels WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="session")
async def test_register_channel_without_actor_params(setup):
    """POST /notifications/channels with body carrying ONLY channel_type/config/
    priority (no user_id/org_id) → 200, and the row is created for the current
    user with org_id stamped from current_user (not from the client)."""
    u1, org = setup["u1"], setup["org"]
    pool = setup["pool"]

    override_identity(app, user_id=u1, org_id=org)
    try:
        async with _client() as c:
            r = await c.post(
                "/api/v1/notifications/channels",
                json={
                    "channel_type": "telegram",
                    "config": {"chat_id": "12345"},
                    "priority": 7,
                },
            )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user_id"] == str(u1), data
        assert data["org_id"] == str(org), data
        assert data["channel_type"] == "telegram"
        assert data["config"] == {"chat_id": "12345"}
        assert data["priority"] == 7
    finally:
        clear_identity(app)

    # Real row exists in Postgres for the current user.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, org_id, channel_type, priority "
            "FROM notification_channels WHERE user_id = $1 AND channel_type = 'telegram'",
            u1,
        )
    assert row is not None, "channel row not persisted"
    assert row["user_id"] == u1
    assert row["org_id"] == org
    assert row["priority"] == 7


@pytest.mark.asyncio(loop_scope="session")
async def test_list_and_count_only_current_user(setup):
    """Seed notifications for u1 (2) and u2 (1). GET /in-app-notifications and
    /unread-count with NO actor params return ONLY the current user's rows."""
    engine = setup["engine"]
    u1, u2, org = setup["u1"], setup["u2"], setup["org"]
    svc = engine.in_app_notification_service

    n1 = await svc.create(user_id=u1, org_id=org, type="system", title="u1 first")
    n2 = await svc.create(user_id=u1, org_id=org, type="system", title="u1 second")
    n_other = await svc.create(user_id=u2, org_id=org, type="system", title="u2 only")

    # As u1: sees exactly its two, never u2's.
    override_identity(app, user_id=u1, org_id=org)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/in-app-notifications")
            assert r.status_code == 200, r.text
            ids = {n["id"] for n in r.json()}
            assert ids == {str(n1.id), str(n2.id)}, r.json()
            assert str(n_other.id) not in ids

            rc = await c.get("/api/v1/in-app-notifications/unread-count")
            assert rc.status_code == 200, rc.text
            assert rc.json()["count"] == 2, rc.json()
    finally:
        clear_identity(app)

    # As u2: sees exactly its one, isolation holds the other direction.
    override_identity(app, user_id=u2, org_id=org)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/in-app-notifications")
            assert r.status_code == 200, r.text
            ids = {n["id"] for n in r.json()}
            assert ids == {str(n_other.id)}, r.json()

            rc = await c.get("/api/v1/in-app-notifications/unread-count")
            assert rc.status_code == 200, rc.text
            assert rc.json()["count"] == 1, rc.json()
    finally:
        clear_identity(app)
