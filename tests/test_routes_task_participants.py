"""Route-layer tests via FastAPI TestClient."""
import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import UUID, uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine_init():
    """Initialize the singleton engine once for this test module
    so its asyncpg pools stay attached to a single loop. Close after
    the module so subsequent test modules can re-init on their own loop."""
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    try:
        await engine.close()
    except Exception:
        pass


@pytest_asyncio.fixture(loop_scope="module")
async def setup(engine_init):
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'rt', $1) RETURNING id",
            f"rt_{uuid4().hex[:8]}",
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'rt_c', 'C', 'x', $2) RETURNING id",
            org, f"rt_c_{uuid4()}@test.local",
        )
        assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'rt_a', 'A', 'x', $2) RETURNING id",
            org, f"rt_a_{uuid4()}@test.local",
        )
        part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'rt_p', 'P', 'x', $2) RETURNING id",
            org, f"rt_p_{uuid4()}@test.local",
        )
    yield {"engine": engine, "org": org, "creator": creator, "assignee": assignee, "part": part}
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_participants WHERE task_id IN "
            "(SELECT id FROM tasks WHERE org_id = $1)", org,
        )
        await conn.execute("DELETE FROM in_app_notifications WHERE org_id = $1", org)
        await conn.execute("DELETE FROM notification_log WHERE user_id IN "
                           "(SELECT id FROM users WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM notification_channels WHERE user_id IN "
                           "(SELECT id FROM users WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM messages WHERE chat_id IN "
                           "(SELECT id FROM chats WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM chats WHERE org_id = $1", org)
        await conn.execute("DELETE FROM task_events WHERE task_id IN "
                           "(SELECT id FROM tasks WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _auth_dep_override(user_id, org_id, is_admin=False):
    """FastAPI dependency override for get_current_user."""
    from src.engine.routes.auth import get_current_user
    async def _override():
        return {"user_id": user_id, "org_id": org_id, "is_admin": is_admin, "is_head": False}
    return get_current_user, _override


@pytest.mark.asyncio(loop_scope="module")
async def test_post_participant_201_and_409_on_duplicate(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
    )
    dep, override = _auth_dep_override(setup["creator"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.post(
                f"/api/v1/tasks/{task.id}/participants",
                json={"user_id": str(setup["part"])},
            )
            assert r1.status_code == 201, r1.text
            assert r1.json()["id"] == str(setup["part"])

            r2 = await c.post(
                f"/api/v1/tasks/{task.id}/participants",
                json={"user_id": str(setup["part"])},
            )
            assert r2.status_code == 409, r2.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_delete_participant_204_then_404(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    dep, override = _auth_dep_override(setup["creator"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.delete(f"/api/v1/tasks/{task.id}/participants/{setup['part']}")
            assert r1.status_code == 204, r1.text
            r2 = await c.delete(f"/api/v1/tasks/{task.id}/participants/{setup['part']}")
            assert r2.status_code == 404, r2.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_get_participating_returns_participant_tasks(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    dep, override = _auth_dep_override(setup["part"], setup["org"])
    app.dependency_overrides[dep] = override
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/tasks/participating")
            assert r.status_code == 200, r.text
            ids = [t["id"] for t in r.json()]
            assert str(task.id) in ids
            entry = next(t for t in r.json() if t["id"] == str(task.id))
            assert any(p["id"] == str(setup["part"]) for p in entry.get("participants", []))
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio(loop_scope="module")
async def test_get_done_unions_roles(setup):
    eng = setup["engine"]
    task = await eng.task_service.create(
        org_id=setup["org"], title="T",
        assignee_user_id=setup["assignee"], created_by_user_id=setup["creator"],
        participant_user_ids=[setup["part"]],
    )
    from src.engine.models.user import User
    a_user = User(id=setup["assignee"], org_id=setup["org"], username="rt_a", name="A", password_hash="x")
    c_user = User(id=setup["creator"], org_id=setup["org"], username="rt_c", name="C", password_hash="x")
    await eng.task_service.take_task(task.id, a_user)
    await eng.task_service.mark_done(task.id, a_user)
    await eng.task_service.accept_task(task.id, c_user)

    for who in (setup["creator"], setup["assignee"], setup["part"]):
        dep, override = _auth_dep_override(who, setup["org"])
        app.dependency_overrides[dep] = override
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/api/v1/tasks/done")
                assert r.status_code == 200, r.text
                ids = [t["id"] for t in r.json()]
                assert str(task.id) in ids, f"expected for user {who}"
        finally:
            app.dependency_overrides.clear()
