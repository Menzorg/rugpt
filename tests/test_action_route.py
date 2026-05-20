"""Integration test for the action dispatcher route.

Real DB. Fixture registers a `__test_action` directly on the engine's
action_registry (production registry stays empty per bootstrap stub).
Verifies 200/400/401/403/404 contracts.
"""
import os
import pytest
import pytest_asyncio
from uuid import uuid4

import asyncpg
from httpx import AsyncClient, ASGITransport
from pydantic import BaseModel

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.actions.registry import ActionDefinition
from src.engine.routes.auth import create_token


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


class _TestActionParams(BaseModel):
    target_id: str


async def _test_handler(engine, user, params: _TestActionParams) -> dict:
    return {"ok": True, "id": params.target_id, "user_id": str(user.id)}


def _allow_active(user, params) -> bool:
    return bool(getattr(user, "is_active", True))


def _admin_only(user, params) -> bool:
    return bool(getattr(user, "is_admin", False))


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'a', $1) "
            "RETURNING id", f"action_{uuid4().hex[:8]}"
        )
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org_id, f"u_{uuid4().hex[:6]}", f"u_{uuid4()}@t.local"
        )

    engine = get_engine_service()
    await engine.initialize()

    # Register test-only action types (cleaned up at end of fixture).
    engine.action_registry.register(ActionDefinition(
        action_type="__test_action",
        handler=_test_handler,
        params_schema=_TestActionParams,
        permission=_allow_active,
    ))
    engine.action_registry.register(ActionDefinition(
        action_type="__test_admin_only",
        handler=_test_handler,
        params_schema=_TestActionParams,
        permission=_admin_only,
    ))

    token = create_token(user_id, org_id, is_admin=False)
    yield {"user_id": str(user_id), "org_id": str(org_id), "token": token, "pool": pool}

    # Cleanup
    engine.action_registry._defs.pop("__test_action", None)
    engine.action_registry._defs.pop("__test_admin_only", None)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_dispatch_happy_path(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={"target_id": "abc-123"},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["id"] == "abc-123"


@pytest.mark.asyncio(loop_scope="module")
async def test_dispatch_unknown_returns_404(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/does_not_exist",
            json={},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio(loop_scope="module")
async def test_dispatch_invalid_params_returns_400(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={},  # missing target_id
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio(loop_scope="module")
async def test_dispatch_permission_denied_returns_403(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_admin_only",
            json={"target_id": "abc"},
            headers={"Authorization": f"Bearer {env['token']}"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio(loop_scope="module")
async def test_dispatch_unauthenticated_returns_401(env):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/v1/actions/__test_action",
            json={"target_id": "abc"},
            # no Authorization header
        )
    assert r.status_code == 401
