"""Storage tests for task_participants. Uses real Postgres via env var."""
import os
import pytest
import pytest_asyncio
from uuid import UUID, uuid4

import asyncpg

from src.engine.storage.task_participant_storage import TaskParticipantStorage
from src.engine.models.task_participant import TaskParticipant


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def fixtures():
    """Create org/users/task for tests; return ids and clean up on teardown."""
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_slug = f"p_org_{uuid4().hex[:8]}"
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'p_org', $1) RETURNING id",
            org_slug,
        )
        creator_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, email, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_creator', 'Creator', $2, 'x') RETURNING id",
            org_id, f"p_creator_{uuid4()}@test.local",
        )
        u1 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, email, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_u1', 'U1', $2, 'x') RETURNING id",
            org_id, f"p_u1_{uuid4()}@test.local",
        )
        u2 = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, email, password_hash) "
            "VALUES (gen_random_uuid(), $1, 'p_u2', 'U2', $2, 'x') RETURNING id",
            org_id, f"p_u2_{uuid4()}@test.local",
        )
        u3_inactive = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, email, password_hash, is_active) "
            "VALUES (gen_random_uuid(), $1, 'p_u3', 'U3', $2, 'x', false) RETURNING id",
            org_id, f"p_u3_{uuid4()}@test.local",
        )
        task_id = await conn.fetchval(
            "INSERT INTO tasks (id, org_id, title, assignee_user_id, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'T', $2, $3) RETURNING id",
            org_id, u1, creator_id,
        )
    yield {
        "org_id": org_id, "task_id": task_id,
        "creator_id": creator_id, "u1": u1, "u2": u2, "u3_inactive": u3_inactive,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio
async def test_add_and_list(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        added = await storage.add(
            task_id=fixtures["task_id"], user_id=fixtures["u2"],
            added_by_user_id=fixtures["creator_id"],
        )
        assert added.user_id == fixtures["u2"]

        rows = await storage.list_active_user_dicts(fixtures["task_id"])
        assert len(rows) == 1
        assert rows[0]["id"] == fixtures["u2"]
        assert rows[0]["name"] == "U2"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_duplicate_raises_unique_violation(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        with pytest.raises(asyncpg.UniqueViolationError):
            await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_remove_returns_true_on_match_false_on_missing(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        assert await storage.remove(fixtures["task_id"], fixtures["u2"]) is True
        assert await storage.remove(fixtures["task_id"], fixtures["u2"]) is False
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_active_user_dicts_filters_inactive_users(fixtures):
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        await storage.add(fixtures["task_id"], fixtures["u3_inactive"], fixtures["creator_id"])
        rows = await storage.list_active_user_dicts(fixtures["task_id"])
        ids = [r["id"] for r in rows]
        assert fixtures["u2"] in ids
        assert fixtures["u3_inactive"] not in ids
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_list_user_ids_returns_all_including_inactive(fixtures):
    """list_user_ids is for project-chat sync — needs ALL participants regardless of is_active."""
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        await storage.add(fixtures["task_id"], fixtures["u3_inactive"], fixtures["creator_id"])
        ids = await storage.list_user_ids(fixtures["task_id"])
        assert set(ids) == {fixtures["u2"], fixtures["u3_inactive"]}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_get_for_tasks_bulk(fixtures):
    """Bulk fetch participants for multiple tasks at once (used by list endpoints)."""
    storage = TaskParticipantStorage(DSN)
    await storage.init()
    try:
        await storage.add(fixtures["task_id"], fixtures["u2"], fixtures["creator_id"])
        result = await storage.get_for_tasks([fixtures["task_id"]])
        assert fixtures["task_id"] in result
        assert result[fixtures["task_id"]][0]["id"] == fixtures["u2"]
        assert result[fixtures["task_id"]][0]["name"] == "U2"
    finally:
        await storage.close()
