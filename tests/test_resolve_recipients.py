import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4

from src.engine.services.engine_service import EngineService

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    e = EngineService()
    await e.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'rr', $1) RETURNING id",
            f"rr_{uuid4().hex[:8]}",
        )
        users = {}
        for tag in ("creator", "assignee", "p1", "p2", "inactive"):
            is_active = tag != "inactive"
            uid = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, $4) RETURNING id",
                org, f"rr_{tag}", f"rr_{tag}_{uuid4()}@test.local", is_active,
            )
            users[tag] = uid
    yield {"engine": e, "org": org, **users}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_participants WHERE task_id IN "
                           "(SELECT id FROM tasks WHERE org_id = $1)", org)
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
    await e.close()


@pytest.mark.asyncio
async def test_resolve_recipients_unions_creator_assignee_participants(env):
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["p1"], env["p2"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task)
    ids = {u.id for u in rec}
    assert ids == {env["creator"], env["assignee"], env["p1"], env["p2"]}


@pytest.mark.asyncio
async def test_resolve_recipients_excludes_actor(env):
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["p1"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task, exclude_user_id=env["assignee"])
    ids = {u.id for u in rec}
    assert env["assignee"] not in ids
    assert ids == {env["creator"], env["p1"]}


@pytest.mark.asyncio
async def test_resolve_recipients_filters_inactive(env):
    """Inactive user must be filtered out even if listed in task_participants."""
    svc = env["engine"].task_service
    tns = env["engine"].task_notification_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["inactive"]],
    )
    task = await svc.get(task.id)
    rec = await tns._resolve_recipients(task)
    ids = {u.id for u in rec}
    assert env["inactive"] not in ids
