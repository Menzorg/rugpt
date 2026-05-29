"""Plan B Task 5: identity migration for routes/users.py.

Proves the user-management route handlers derive the ACTOR (the requesting
admin/self) from current_user (device-signed, FAIL-CLOSED via get_current_user)
and that the TARGET user is addressed via the renamed `{target_user_id}` path
segment. Uses the lightweight `override_identity` override (route logic depends
only on current_user, not the signature path) and inserts real rows via asyncpg
so assertions verify actual behaviour, not just status codes.

Coverage:
  - POST /users (create)        → org of the new user is derived from the
                                  creating admin's current_user["org_id"]; the
                                  request body carries NO actor org_id.
  - POST /users/{target_user_id}/role (admin op) → admin assigns a role to the
                                  TARGET user; actor (admin) comes from
                                  current_user; renamed path honoured.
  - DELETE /users/{target_user_id} (admin op) → admin deactivates the TARGET;
                                  renamed path honoured; real is_active flips.
  - POST /users/{target_user_id}/password (self-only guard) → a non-admin acting
                                  on ANOTHER user's target_user_id → 403.
  - POST /users/{target_user_id}/password (self) → user changes OWN password,
                                  new hash actually verifies.
"""
import os

import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
from src.engine.services.users_service import UsersService
from tests.zt_helpers import override_identity, clear_identity

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def engine_init():
    """Initialize the singleton engine once for this test module."""
    engine = get_engine_service()
    await engine.initialize()
    yield engine
    try:
        await engine.close()
    except Exception:
        pass


@pytest_asyncio.fixture(loop_scope="session")
async def setup(engine_init):
    """Create org + admin + employee + a role, plus a second org for isolation.

    Seeds a real bcrypt password hash for the employee so the self password
    change can be verified end-to-end. Cleanup wipes users/roles/orgs in FK-safe
    order, including any user created by the create-user test.
    """
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    known_password = "OldP@ss123"
    hashed = UsersService(engine.user_storage)._hash_password(known_password)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'zt-users', $1) RETURNING id",
            f"ztu_{uuid4().hex[:8]}",
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, 'ADMIN', 'x', $3, true, true) RETURNING id",
            org, f"ztu_adm_{uuid4().hex[:6]}", f"ztu_adm_{uuid4()}@test.local",
        )
        employee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, 'EMP', $3, $4, false, true) RETURNING id",
            org, f"ztu_emp_{uuid4().hex[:6]}", hashed, f"ztu_emp_{uuid4()}@test.local",
        )
        # A second non-admin (the would-be victim of a cross-user password change).
        # Seeded with a real bcrypt hash of `known_password` so we can prove the
        # hash is untouched after a forbidden cross-user attempt.
        other = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, 'OTHER', $3, $4, false, true) RETURNING id",
            org, f"ztu_oth_{uuid4().hex[:6]}", hashed, f"ztu_oth_{uuid4()}@test.local",
        )
        role = await conn.fetchval(
            "INSERT INTO roles (id, org_id, name, code, system_prompt) "
            "VALUES (gen_random_uuid(), $1, $2, $3, 'sp') RETURNING id",
            org, f"ztu_role_{uuid4().hex[:6]}", f"ztu_code_{uuid4().hex[:6]}",
        )
    yield {
        "engine": engine,
        "pool": pool,
        "org": org,
        "admin": admin,
        "employee": employee,
        "other": other,
        "role": role,
        "known_password": known_password,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM roles WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="session")
async def test_create_user_org_derived_from_admin(setup):
    """POST /users with NO org_id in body → new user lands in the admin's org.

    The CreateUserRequest model carries no org_id; the route derives it from
    current_user["org_id"] (the creating admin's org). Asserts the created row's
    org_id actually equals the admin's org.
    """
    override_identity(app, user_id=setup["admin"], org_id=setup["org"], is_admin=True)
    new_username = f"ztu_new_{uuid4().hex[:6]}"
    body = {
        "name": "Created User",
        "username": new_username,
        "email": f"{new_username}@example.com",
        "password": "Created@123",
    }
    try:
        async with _client() as c:
            r = await c.post("/api/v1/users", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["username"] == new_username
        assert data["org_id"] == str(setup["org"]), data
        assert data["is_admin"] is False  # API never creates admins
    finally:
        clear_identity(app)
        # cleanup of the created user happens via fixture (DELETE by org_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_assign_role_admin_target_path(setup):
    """POST /users/{target_user_id}/role as admin → role assigned to the TARGET.

    The actor (admin) is taken from current_user; the target is the renamed path
    segment. Asserts the employee's row actually carries the role afterwards.
    """
    override_identity(app, user_id=setup["admin"], org_id=setup["org"], is_admin=True)
    try:
        async with _client() as c:
            r = await c.post(
                f"/api/v1/users/{setup['employee']}/role",
                json={"role_id": str(setup["role"])},
            )
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        # Real effect: employee row now has the role.
        target = await setup["engine"].user_storage.get_by_id(setup["employee"])
        assert str(target.role_id) == str(setup["role"]), target
    finally:
        # unassign to keep fixture state clean for other tests
        await setup["engine"].user_storage.assign_role(setup["employee"], None)
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivate_user_admin_target_path(setup):
    """DELETE /users/{target_user_id} as admin → TARGET deactivated.

    Asserts the target's is_active flag actually flips to False.
    """
    override_identity(app, user_id=setup["admin"], org_id=setup["org"], is_admin=True)
    try:
        async with _client() as c:
            r = await c.delete(f"/api/v1/users/{setup['other']}")
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        target = await setup["engine"].user_storage.get_by_id(setup["other"])
        assert target.is_active is False, target
    finally:
        # reactivate to keep fixture state consistent
        target = await setup["engine"].user_storage.get_by_id(setup["other"])
        if target:
            target.is_active = True
            await setup["engine"].user_storage.update(target)
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_change_password_other_user_forbidden(setup):
    """POST /users/{target_user_id}/password where target != self → 403.

    A non-admin employee tries to change ANOTHER user's password. The self-only
    guard (target_user_id must equal current_user["user_id"]) must reject it,
    and the victim's password must remain unchanged.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    try:
        async with _client() as c:
            r = await c.post(
                f"/api/v1/users/{setup['other']}/password",
                json={"current_password": "x", "new_password": "Hacked@123"},
            )
        assert r.status_code == 403, r.text
        # Victim's password hash untouched: original still verifies, attacker's does not.
        svc = UsersService(setup["engine"].user_storage)
        assert await svc.verify_password(setup["other"], setup["known_password"]) is True
        assert await svc.verify_password(setup["other"], "Hacked@123") is False
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_change_own_password(setup):
    """POST /users/{target_user_id}/password where target == self → 200.

    The employee changes their OWN password using the correct current password;
    the new password must actually verify afterwards.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    new_password = "FreshP@ss456"
    try:
        async with _client() as c:
            r = await c.post(
                f"/api/v1/users/{setup['employee']}/password",
                json={
                    "current_password": setup["known_password"],
                    "new_password": new_password,
                },
            )
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        svc = UsersService(setup["engine"].user_storage)
        assert await svc.verify_password(setup["employee"], new_password) is True
        assert await svc.verify_password(setup["employee"], setup["known_password"]) is False
    finally:
        clear_identity(app)
