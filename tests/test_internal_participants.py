"""Internal read-only route: GET /api/v1/internal/chats/{chat_id}/participants.

The route carries NO signature (it lives behind the network boundary, not under
/web). Asserts it returns the seeded chat's participant ids, and 404 for an
unknown chat id.
"""
import os
import uuid

import pytest
import pytest_asyncio
import asyncpg
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service()
    get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval(
            "INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'ip',$1) RETURNING id",
            f"ip_{uuid.uuid4().hex[:8]}")
        u1 = await c.fetchval(
            "INSERT INTO users (id,org_id,username,name,password_hash,email) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3) RETURNING id",
            org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
        u2 = await c.fetchval(
            "INSERT INTO users (id,org_id,username,name,password_hash,email) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3) RETURNING id",
            org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
        chat = await c.fetchval(
            """
            INSERT INTO chats (id, org_id, type, participants, created_by, is_active)
            VALUES (gen_random_uuid(), $1, 'direct', ARRAY[$2::text, $3::text], $4, true)
            RETURNING id
            """,
            org, str(u1), str(u2), u1)
    await pool.close()
    return {"chat": str(chat), "u1": str(u1), "u2": str(u2)}


@pytest.mark.asyncio(loop_scope="module")
async def test_participants_returns_ids(env):
    """A seeded chat returns its participant ids."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/v1/internal/chats/{env['chat']}/participants")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["participants"]) == {env["u1"], env["u2"]}


@pytest.mark.asyncio(loop_scope="module")
async def test_participants_unknown_chat_404(env):
    """An unknown chat id returns 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/v1/internal/chats/{uuid.uuid4()}/participants")
    assert r.status_code == 404, r.text
