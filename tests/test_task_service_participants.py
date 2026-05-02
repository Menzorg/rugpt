import os
import pytest
import pytest_asyncio
import asyncpg
from uuid import UUID, uuid4

from src.engine.services.engine_service import EngineService
from src.engine.models.user import User

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture
async def env():
    os.environ["DATABASE_URL"] = DSN
    e = EngineService()
    await e.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'tsv', $1) RETURNING id",
            f"tsv_{uuid4().hex[:8]}",
        )
        creator = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsv_c', 'C', 'x', $2) RETURNING id",
            org, f"tsv_c_{uuid4()}@test.local",
        )
        assignee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsv_a', 'A', 'x', $2) RETURNING id",
            org, f"tsv_a_{uuid4()}@test.local",
        )
        part = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsv_p', 'P', 'x', $2) RETURNING id",
            org, f"tsv_p_{uuid4()}@test.local",
        )
        outsider = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, 'tsv_o', 'O', 'x', $2) RETURNING id",
            org, f"tsv_o_{uuid4()}@test.local",
        )
    yield {"engine": e, "org": org, "creator": creator,
           "assignee": assignee, "part": part, "outsider": outsider}
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM task_participants WHERE task_id IN "
            "(SELECT id FROM tasks WHERE org_id = $1)", org,
        )
        await conn.execute("DELETE FROM messages WHERE chat_id IN "
                           "(SELECT id FROM chats WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM chats WHERE org_id = $1", org)
        await conn.execute("DELETE FROM task_events WHERE task_id IN "
                           "(SELECT id FROM tasks WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM in_app_notifications WHERE org_id = $1", org)
        await conn.execute("DELETE FROM notification_log WHERE user_id IN "
                           "(SELECT id FROM users WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM notification_channels WHERE user_id IN "
                           "(SELECT id FROM users WHERE org_id = $1)", org)
        await conn.execute("DELETE FROM tasks WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


def _user(uid, org, *, is_admin=False, is_head=False):
    return User(id=uid, org_id=org, username="x", name="x",
                password_hash="x", is_admin=is_admin, is_head=is_head)


@pytest.mark.asyncio
async def test_add_participant_happy_path(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    added = await svc.add_participant(task.id, env["part"], creator_user)
    assert added["id"] == env["part"]


@pytest.mark.asyncio
async def test_add_participant_rejects_assignee_or_creator(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    with pytest.raises(ValueError, match="already assignee"):
        await svc.add_participant(task.id, env["assignee"], creator_user)
    with pytest.raises(ValueError, match="already creator"):
        await svc.add_participant(task.id, env["creator"], creator_user)


@pytest.mark.asyncio
async def test_add_participant_only_creator_or_admin(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    outsider = _user(env["outsider"], env["org"])
    with pytest.raises(PermissionError):
        await svc.add_participant(task.id, env["part"], outsider)
    admin = _user(env["outsider"], env["org"], is_admin=True)
    out = await svc.add_participant(task.id, env["part"], admin)
    assert out["id"] == env["part"]


@pytest.mark.asyncio
async def test_remove_participant_happy_path_and_missing(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
    )
    creator_user = _user(env["creator"], env["org"])
    await svc.add_participant(task.id, env["part"], creator_user)
    assert await svc.remove_participant(task.id, env["part"], creator_user) is True
    assert await svc.remove_participant(task.id, env["part"], creator_user) is False


@pytest.mark.asyncio
async def test_create_with_participant_user_ids(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    rows = await env["engine"].task_participant_storage.list_active_user_dicts(task.id)
    assert any(r["id"] == env["part"] for r in rows)


@pytest.mark.asyncio
async def test_create_filters_assignee_and_creator_from_participants(env):
    """Even if caller mistakenly passes assignee/creator in participant_user_ids, they must be silently filtered."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"],
        created_by_user_id=env["creator"],
        participant_user_ids=[env["assignee"], env["creator"], env["part"]],
    )
    rows = await env["engine"].task_participant_storage.list_active_user_dicts(task.id)
    ids = {r["id"] for r in rows}
    assert env["part"] in ids
    assert env["assignee"] not in ids
    assert env["creator"] not in ids


@pytest.mark.asyncio
async def test_assignee_swap_old_becomes_participant_new_leaves(env):
    """Reassign auto-swap: old assignee -> participant, new assignee -> removed from participants."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    # Reassign to part (who was a participant)
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["part"],
        actor_user_id=env["creator"],
    )
    rows = await env["engine"].task_participant_storage.list_user_ids(task.id)
    # part should no longer be participant (now assignee)
    assert env["part"] not in rows
    # old assignee should be participant
    assert env["assignee"] in rows


@pytest.mark.asyncio
async def test_assignee_swap_skips_creator(env):
    """If old assignee == creator, do NOT add as participant."""
    svc = env["engine"].task_service
    # creator is also assignee (degenerate but possible)
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["creator"], created_by_user_id=env["creator"],
    )
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["assignee"],
        actor_user_id=env["creator"],
    )
    rows = await env["engine"].task_participant_storage.list_user_ids(task.id)
    # creator stays as creator only, not duplicated as participant
    assert env["creator"] not in rows


@pytest.mark.asyncio
async def test_list_participating(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    entries = await svc.list_participating(env["part"])
    ids = [e["task"].id for e in entries]
    assert task.id in ids


@pytest.mark.asyncio
async def test_list_done_returns_done_for_all_roles(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["assignee"], created_by_user_id=env["creator"],
        participant_user_ids=[env["part"]],
    )
    # Move task to done via take + mark_done + accept
    a = _user(env["assignee"], env["org"])
    c = _user(env["creator"], env["org"])
    await svc.take_task(task.id, a)
    await svc.mark_done(task.id, a)
    await svc.accept_task(task.id, c)
    for u in (env["creator"], env["assignee"], env["part"]):
        entries = await svc.list_done(u)
        assert any(e["task"].id == task.id for e in entries), f"missing for {u}"
