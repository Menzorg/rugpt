import os, pytest, pytest_asyncio, asyncpg
from uuid import uuid4
from src.engine.storage.task_storage import TaskStorage
from src.engine.storage.task_participant_storage import TaskParticipantStorage

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def fixtures():
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'tsp', $1) RETURNING id",
            f"tsp_{uuid4().hex[:8]}",
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsp_c', 'C', 'x', $2) RETURNING id",
            org, f"tsp_c_{uuid4()}@test.local",
        )
        u_assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsp_a', 'A', 'x', $2) RETURNING id",
            org, f"tsp_a_{uuid4()}@test.local",
        )
        u_part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsp_p', 'P', 'x', $2) RETURNING id",
            org, f"tsp_p_{uuid4()}@test.local",
        )
        u_other = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsp_o', 'O', 'x', $2) RETURNING id",
            org, f"tsp_o_{uuid4()}@test.local",
        )
        project = await conn.fetchval(
            "INSERT INTO projects (id, org_id, name, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'P', $2) RETURNING id",
            org, creator,
        )
        t1 = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, project_id) "
            "VALUES (gen_random_uuid(), $1, 'T1', $2, $3, $4) RETURNING id",
            org, u_assignee, creator, project,
        )
        t2 = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, project_id, status) "
            "VALUES (gen_random_uuid(), $1, 'T2', $2, $3, $4, 'done') RETURNING id",
            org, u_assignee, creator, project,
        )
    yield {
        "org": org, "creator": creator, "u_assignee": u_assignee,
        "u_part": u_part, "u_other": u_other, "project": project,
        "t1": t1, "t2": t2,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM projects WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_creator(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["creator"]) is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_assignee(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_assignee"]) is True
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_via_participant(fixtures):
    """User who is participant in any active task of the project should count."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_part"]) is True
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_user_has_any_active_task_in_project_negative(fixtures):
    storage = TaskStorage(DSN); await storage.init()
    try:
        assert await storage.user_has_any_active_task_in_project(fixtures["project"], fixtures["u_other"]) is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_by_participant_with_priority_excludes_done(fixtures):
    """Default include_done=False — only active, non-done tasks."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])  # status='created'
        await ps.add(fixtures["t2"], fixtures["u_part"], fixtures["creator"])  # status='done'
        rows = await storage.list_by_participant_with_priority(fixtures["u_part"])
        ids = [e["task"].id for e in rows]
        assert fixtures["t1"] in ids
        assert fixtures["t2"] not in ids
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_list_done_for_user_unions_all_roles(fixtures):
    """Done list includes done tasks where user is creator OR assignee OR participant."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])

        # creator
        rows = await storage.list_done_for_user(fixtures["creator"])
        assert any(e["task"].id == fixtures["t2"] for e in rows)

        # assignee
        rows = await storage.list_done_for_user(fixtures["u_assignee"])
        assert any(e["task"].id == fixtures["t2"] for e in rows)

        # other (no relation) - empty
        rows = await storage.list_done_for_user(fixtures["u_other"])
        assert rows == []
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_list_by_participant_with_priority_sorts_by_priority(fixtures):
    """Higher priority must come first in /participating list."""
    import asyncpg as _asyncpg
    pool = await _asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        t_low = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, priority) "
            "VALUES (gen_random_uuid(), $1, 'Low', $2, $3, 1) RETURNING id",
            fixtures["org"], fixtures["u_assignee"], fixtures["creator"],
        )
        t_high = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id, priority) "
            "VALUES (gen_random_uuid(), $1, 'High', $2, $3, 3) RETURNING id",
            fixtures["org"], fixtures["u_assignee"], fixtures["creator"],
        )
    await pool.close()

    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(t_low, fixtures["u_part"], fixtures["creator"])
        await ps.add(t_high, fixtures["u_part"], fixtures["creator"])
        rows = await storage.list_by_participant_with_priority(fixtures["u_part"])
        ids_in_order = [e["task"].id for e in rows if e["task"].id in (t_low, t_high)]
        assert ids_in_order == [t_high, t_low]
    finally:
        await ps.close(); await storage.close()


@pytest.mark.asyncio
async def test_list_by_participant_with_priority_include_done_true(fixtures):
    """include_done=True returns both active and done participated tasks."""
    ps = TaskParticipantStorage(DSN); await ps.init()
    storage = TaskStorage(DSN); await storage.init()
    try:
        await ps.add(fixtures["t1"], fixtures["u_part"], fixtures["creator"])  # status='created'
        await ps.add(fixtures["t2"], fixtures["u_part"], fixtures["creator"])  # status='done'
        rows = await storage.list_by_participant_with_priority(
            fixtures["u_part"], include_done=True,
        )
        ids = {e["task"].id for e in rows}
        assert {fixtures["t1"], fixtures["t2"]} <= ids
    finally:
        await ps.close(); await storage.close()
