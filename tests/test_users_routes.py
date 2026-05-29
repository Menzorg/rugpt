"""Integration tests for users routes / storage.

Reproduces the fixture pattern of test_resolve_recipients.py.
Skips if DSN unreachable.
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
    org_id = None
    dept_id = None
    user_id = None
    uname = f"dt_{uuid4().hex[:8]}"
    try:
        async with pool.acquire() as conn:
            org_id = await conn.fetchval(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (gen_random_uuid(), 'dt', $1) RETURNING id",
                f"dt_{uuid4().hex[:8]}",
            )
            dept_id = await conn.fetchval(
                "INSERT INTO departments (id, org_id, name) "
                "VALUES (gen_random_uuid(), $1, $2) RETURNING id",
                org_id, "Юристы",
            )
            user_id = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active, department_id) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true, $4) RETURNING id",
                org_id, uname, f"{uname}@test.local", dept_id,
            )
        yield {
            "engine": e,
            "org_id": org_id,
            "dept_id": dept_id,
            "user_id": user_id,
            "username": uname,
        }
    finally:
        async with pool.acquire() as conn2:
            if user_id:
                await conn2.execute("DELETE FROM users WHERE id = $1", user_id)
            if dept_id:
                await conn2.execute("DELETE FROM departments WHERE id = $1", dept_id)
            if org_id:
                await conn2.execute("DELETE FROM organizations WHERE id = $1", org_id)
        await pool.close()
        await e.close()


@pytest.mark.asyncio
async def test_get_by_username_includes_department_name(env):
    """get_by_username returns User with department_name populated via JOIN."""
    engine = env["engine"]
    user = await engine.user_storage.get_by_username(env["username"], env["org_id"])
    assert user is not None
    assert user.department_id == env["dept_id"]
    assert user.department_name == "Юристы"


@pytest.mark.asyncio
async def test_get_by_username_no_department(env):
    """User without department — department_name should be None, not raise."""
    engine = env["engine"]
    # detach department from the seeded user
    pool = await asyncpg.create_pool(DSN)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET department_id = NULL WHERE id = $1",
                env["user_id"],
            )
        user = await engine.user_storage.get_by_username(env["username"], env["org_id"])
        assert user is not None
        assert user.department_id is None
        assert user.department_name is None
    finally:
        await pool.close()
