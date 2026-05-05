"""Bell-нотификация после take_task для creator (smoke).

Реальная БД (как test_resolve_recipients.py). Скипается если нет DSN.
"""
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
            "VALUES (gen_random_uuid(), 'bell', $1) RETURNING id",
            f"bell_{uuid4().hex[:8]}",
        )
        users = {}
        for tag in ("creator", "assignee"):
            uid = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
                org, f"bell_{tag}", f"bell_{tag}_{uuid4()}@test.local",
            )
            users[tag] = uid
        yield {
            "engine": e, "org_id": org, "creator": users["creator"],
            "assignee": users["assignee"], "pool": pool,
        }
        async with pool.acquire() as conn2:
            await conn2.execute(
                "DELETE FROM in_app_notifications WHERE user_id = ANY($1::uuid[])",
                list(users.values()),
            )
            # task_service.create() auto-создаёт task-чат → есть FK chats.task_id → tasks.id.
            # Чистим в обратном порядке: messages → chats → tasks.
            await conn2.execute(
                "DELETE FROM messages WHERE chat_id IN "
                "(SELECT id FROM chats WHERE task_id IN "
                "(SELECT id FROM tasks WHERE org_id = $1))",
                org,
            )
            await conn2.execute(
                "DELETE FROM chats WHERE task_id IN "
                "(SELECT id FROM tasks WHERE org_id = $1)",
                org,
            )
            await conn2.execute("DELETE FROM tasks WHERE org_id = $1", org)
            await conn2.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", list(users.values()),
            )
            await conn2.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


@pytest.mark.asyncio
async def test_take_task_creates_bell_for_creator(env):
    engine = env["engine"]
    task = await engine.task_service.create(
        org_id=env["org_id"],
        title="bell-test",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
    )
    assignee_user = await engine.user_storage.get_by_id(env["assignee"])
    await engine.task_service.take_task(task.id, assignee_user)

    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT type, title, content, reference_type, reference_id "
            "FROM in_app_notifications "
            "WHERE user_id = $1 AND reference_id = $2 "
            "ORDER BY created_at DESC",
            env["creator"], task.id,
        )

    bell_take = [r for r in rows if r["title"].endswith("взята в работу")]
    assert len(bell_take) == 1, f"expected 1 bell row, got {len(bell_take)}: {rows}"
    r = bell_take[0]
    assert r["type"] == "task_status_change"
    assert r["reference_type"] == "task"
    assert str(r["reference_id"]) == str(task.id)
    assert r["title"] == f"Задача «bell-test» взята в работу"
    assert r["content"]  # non-empty actor name
