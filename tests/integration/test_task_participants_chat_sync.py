"""Integration tests for participant chat synchronization.

Verifies that add_participant/remove_participant/reassign properly sync the task chat
and project chat memberships through the chat_service hooks in TaskService.
"""
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
    e = EngineService()
    await e.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'is', $1) RETURNING id",
            f"is_{uuid4().hex[:8]}",
        )
        users = {}
        for tag in ("c", "a", "p1", "p2"):
            users[tag] = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3) RETURNING id",
                org, f"is_{tag}_{uuid4().hex[:6]}",
                f"is_{tag}_{uuid4()}@test.local",
            )
        project = await conn.fetchval(
            "INSERT INTO projects (id, org_id, name, created_by_user_id) "
            "VALUES (gen_random_uuid(), $1, 'P', $2) RETURNING id",
            org, users["c"],
        )
    yield {"engine": e, "org": org, "project": project, **users}
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
        await conn.execute("DELETE FROM projects WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()
    await e.close()


def _user(uid, org, *, is_admin=False):
    return User(id=uid, org_id=org, username="x", name="x", password_hash="x", is_admin=is_admin)


@pytest.mark.asyncio
async def test_add_participant_appears_in_task_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
    )
    creator = _user(env["c"], env["org"])
    await svc.add_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["p1"] in chat.participants


@pytest.mark.asyncio
async def test_add_participant_appears_in_project_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"],
    )
    creator = _user(env["c"], env["org"])
    await svc.add_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] in chat.participants


@pytest.mark.asyncio
async def test_remove_participant_drops_from_chat(env):
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        participant_user_ids=[env["p1"]],
    )
    creator = _user(env["c"], env["org"])
    await svc.remove_participant(task.id, env["p1"], creator)
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["p1"] not in chat.participants


@pytest.mark.asyncio
async def test_remove_participant_keeps_user_in_project_if_in_other_task(env):
    """User must stay in project chat if still participant of another task in the project."""
    svc = env["engine"].task_service
    creator = _user(env["c"], env["org"])
    t1 = await svc.create(
        org_id=env["org"], title="T1",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"], participant_user_ids=[env["p1"]],
    )
    t2 = await svc.create(
        org_id=env["org"], title="T2",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        project_id=env["project"], participant_user_ids=[env["p1"]],
    )
    await svc.remove_participant(t1.id, env["p1"], creator)
    pchat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] in pchat.participants  # still in t2

    await svc.remove_participant(t2.id, env["p1"], creator)
    pchat = await env["engine"].chat_service.chat_storage.get_by_project_id(env["project"])
    assert env["p1"] not in pchat.participants  # gone now


@pytest.mark.asyncio
async def test_assignee_swap_in_chat(env):
    """After reassign: old assignee stays in chat (now participant), new assignee in chat (now assignee)."""
    svc = env["engine"].task_service
    task = await svc.create(
        org_id=env["org"], title="T",
        assignee_user_id=env["a"], created_by_user_id=env["c"],
        participant_user_ids=[env["p1"]],
    )
    await svc.update(
        task_id=task.id,
        assignee_user_id=env["p1"],
        actor_user_id=env["c"],
    )
    chat = await env["engine"].chat_service.get_task_chat(task.id)
    assert env["a"] in chat.participants  # old assignee, now participant
    assert env["p1"] in chat.participants  # new assignee
    assert env["c"] in chat.participants  # creator
