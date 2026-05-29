"""Plan B Task 8 — Zero Trust identity hardening for organizations / rag / tasks routes.

Verifies:
  * organizations GET on the renamed `/{target_org_id}` path works for an admin
    of that org, and a non-admin / cross-org actor is restricted per the existing
    "see only your own org" guard (404).
  * a tasks route (/tasks/my) derives the actor purely from current_user — no
    actor request param — and scopes results to that actor's tasks.
  * a tasks participant route honours the renamed `target_user_id` body field.
  * rag route exposes NO actor user_id/org_id request param (org/user are taken
    from current_user); the only descriptive param is the optional `user_id`
    filter (filter_user_id). Asserted statically (rag find needs live embeddings).

Real Postgres rows + override_identity (zt_helpers) + AsyncClient/ASGITransport
with a module-scoped event loop, matching the engine's other route tests.
"""
import os
import inspect

import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from tests.zt_helpers import override_identity, clear_identity

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine_init():
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    try:
        await engine.close()
    except Exception:
        pass


@pytest_asyncio.fixture(loop_scope="module")
async def ctx(engine_init):
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'zt', $1) RETURNING id",
            f"zt_{uuid4().hex[:8]}",
        )
        other_org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'zt2', $1) RETURNING id",
            f"zt2_{uuid4().hex[:8]}",
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, 'zt_adm', 'Adm', 'x', $2, true) RETURNING id",
            org, f"zt_adm_{uuid4()}@test.local",
        )
        member = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, 'zt_mem', 'Mem', 'x', $2, false) RETURNING id",
            org, f"zt_mem_{uuid4()}@test.local",
        )
        part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, 'zt_part', 'Part', 'x', $2, false) RETURNING id",
            org, f"zt_part_{uuid4()}@test.local",
        )
    yield {
        "engine": engine, "org": org, "other_org": other_org,
        "admin": admin, "member": member, "part": part,
    }
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_participants WHERE task_id IN "
            "(SELECT id FROM tasks WHERE org_id = $1)", org,
        )
        await conn.execute("DELETE FROM in_app_notifications WHERE org_id = $1", org)
        await conn.execute("DELETE FROM task_events WHERE task_id IN "
                           "(SELECT id FROM tasks WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM messages WHERE chat_id IN "
                           "(SELECT id FROM chats WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM chats WHERE org_id = $1", org)
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::uuid[])",
                           [org, other_org])
    await pool.close()
    clear_identity(app)


# ---------------------------------------------------------------------------
# organizations: renamed path /{target_org_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_org_get_admin_renamed_path(ctx):
    """Admin of the org reads it via the renamed /{target_org_id} path."""
    override_identity(app, user_id=ctx["admin"], org_id=ctx["org"], is_admin=True)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/organizations/{ctx['org']}")
            assert r.status_code == 200, r.text
            assert r.json()["id"] == str(ctx["org"])
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="module")
async def test_org_get_cross_org_404(ctx):
    """Actor scoped to org A cannot read org B — 404 (don't leak existence)."""
    override_identity(app, user_id=ctx["member"], org_id=ctx["org"], is_admin=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/api/v1/organizations/{ctx['other_org']}")
            assert r.status_code == 404, r.text
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="module")
async def test_org_route_uses_target_org_id_template(ctx):
    """The migrated path template is {target_org_id}, not {org_id}."""
    paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/v1/organizations")}
    assert "/api/v1/organizations/{target_org_id}" in paths
    assert "/api/v1/organizations/{org_id}" not in paths


# ---------------------------------------------------------------------------
# tasks: actor derived from current_user (no actor param)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio(loop_scope="module")
async def test_tasks_my_scopes_to_actor(ctx):
    """/tasks/my returns only tasks where current_user is assignee — actor comes
    from current_user, there is no actor request param."""
    eng = ctx["engine"]
    mine = await eng.task_service.create(
        org_id=ctx["org"], title="mine",
        assignee_user_id=ctx["member"], created_by_user_id=ctx["admin"],
    )
    not_mine = await eng.task_service.create(
        org_id=ctx["org"], title="not-mine",
        assignee_user_id=ctx["part"], created_by_user_id=ctx["admin"],
    )

    # tasks routes compare task.org_id (UUID) against current_user["org_id"]
    # directly, so the actor's org_id must be the UUID (the shape get_current_user
    # actually returns), not a stringified copy.
    override_identity(app, user_id=ctx["member"], org_id=ctx["org"], is_admin=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/tasks/my")
            assert r.status_code == 200, r.text
            ids = {t["id"] for t in r.json()}
            assert str(mine.id) in ids
            assert str(not_mine.id) not in ids
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="module")
async def test_add_participant_renamed_body_field(ctx):
    """POST participants honours the renamed target_user_id body field."""
    eng = ctx["engine"]
    task = await eng.task_service.create(
        org_id=ctx["org"], title="with-part",
        assignee_user_id=ctx["member"], created_by_user_id=ctx["admin"],
    )
    override_identity(app, user_id=ctx["admin"], org_id=ctx["org"], is_admin=True)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                f"/api/v1/tasks/{task.id}/participants",
                json={"target_user_id": str(ctx["part"])},
            )
            assert r.status_code == 201, r.text
            assert r.json()["id"] == str(ctx["part"])
    finally:
        clear_identity(app)


# ---------------------------------------------------------------------------
# rag: no actor user_id/org_id request param (static check)
# ---------------------------------------------------------------------------

def test_rag_find_has_no_actor_param():
    """rag.find_docs derives org_id/user_id from current_user; the only user_id
    param is the optional descriptive filter (filter_user_id, alias='user_id')."""
    from src.engine.routes.rag import find_docs
    sig = inspect.signature(find_docs)
    params = set(sig.parameters)
    # No bare actor params:
    assert "org_id" not in params
    assert "user_id" not in params
    # Descriptive filter kept as-is:
    assert "filter_user_id" in params
    assert "current_user" in params
