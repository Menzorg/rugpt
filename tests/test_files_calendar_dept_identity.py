"""Plan B Task 7: identity migration for routes/files.py, calendar.py, departments.py.

Proves the file/calendar/department route handlers derive actor identity from
current_user (device-signed, FAIL-CLOSED via get_current_user) and no longer
accept actor `user_id`/`org_id` as request params. Uses the lightweight
`override_identity` override (route logic depends only on current_user, not the
signature path) and inserts real rows via asyncpg so assertions verify actual
behaviour, not just status codes.

Coverage:
  - GET /files (list)                          → returns ONLY the current user's
                                                  own files (no actor user_id/org_id).
  - GET /calendar/events                       → scopes to the current user's org
                                                  (no actor org_id param).
  - POST /departments/{id}/head/{target_user_id}
                                               → admin assigns a target user as head;
                                                  the target user's is_head flips true.
                                               → non-admin actor → 403.
"""
import os

import pytest
import pytest_asyncio
import asyncpg
from uuid import uuid4
from httpx import AsyncClient, ASGITransport

from src.engine.app import app
from src.engine.services.engine_service import get_engine_service
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
    """Create org + admin + employee + a department the employee belongs to.

    Seed rows so route assertions check real data:
      - employee owns one file; admin owns a different file (employee must NOT see admin's)
      - one calendar event in this org and one event in a SECOND org (must NOT leak)
      - employee.department_id = dept (so set_head can promote them)
    Cleanup wipes files/events/users/depts/orgs in FK-safe order.
    """
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'fcd-id', $1) RETURNING id",
            f"fcd_{uuid4().hex[:8]}",
        )
        other_org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'fcd-other', $1) RETURNING id",
            f"fcd_other_{uuid4().hex[:8]}",
        )
        dept = await conn.fetchval(
            "INSERT INTO departments (id, org_id, name) "
            "VALUES (gen_random_uuid(), $1, 'Eng') RETURNING id",
            org,
        )
        admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'ADM', 'x', $3, true) RETURNING id",
            org, f"fcd_adm_{uuid4().hex[:6]}", f"fcd_adm_{uuid4()}@test.local",
        )
        employee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, department_id) "
            "VALUES (gen_random_uuid(), $1, $2, 'EMP', 'x', $3, $4) RETURNING id",
            org, f"fcd_emp_{uuid4().hex[:6]}", f"fcd_emp_{uuid4()}@test.local", dept,
        )

        # Employee's own file
        emp_file = await conn.fetchval(
            "INSERT INTO user_files "
            "(id, org_id, user_id, uploaded_by_user_id, storage_key, original_filename, "
            " file_type, file_size, content_hash, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, $3, 'emp.txt', 'txt', 3, $4, true) RETURNING id",
            org, employee, f"k_{uuid4().hex}", uuid4().hex,
        )
        # Admin's own file (employee must not see it)
        adm_file = await conn.fetchval(
            "INSERT INTO user_files "
            "(id, org_id, user_id, uploaded_by_user_id, storage_key, original_filename, "
            " file_type, file_size, content_hash, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, $3, 'adm.txt', 'txt', 3, $4, true) RETURNING id",
            org, admin, f"k_{uuid4().hex}", uuid4().hex,
        )

        # Calendar events need a role (FK role_id). Create one per org.
        role = await conn.fetchval(
            "INSERT INTO roles (id, org_id, name, code, system_prompt) "
            "VALUES (gen_random_uuid(), $1, 'R', $2, 'p') RETURNING id",
            org, f"r_{uuid4().hex[:6]}",
        )
        other_role = await conn.fetchval(
            "INSERT INTO roles (id, org_id, name, code, system_prompt) "
            "VALUES (gen_random_uuid(), $1, 'R2', $2, 'p') RETURNING id",
            other_org, f"r_{uuid4().hex[:6]}",
        )
        my_event = await conn.fetchval(
            "INSERT INTO calendar_events (id, org_id, role_id, title, event_type, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, 'MINE', 'one_time', true) RETURNING id",
            org, role,
        )
        other_event = await conn.fetchval(
            "INSERT INTO calendar_events (id, org_id, role_id, title, event_type, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, 'NOT MINE', 'one_time', true) RETURNING id",
            other_org, other_role,
        )
    yield {
        "engine": engine, "pool": pool,
        "org": org, "other_org": other_org, "dept": dept,
        "admin": admin, "employee": employee,
        "emp_file": emp_file, "adm_file": adm_file,
        "my_event": my_event, "other_event": other_event,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM calendar_events WHERE org_id = ANY($1::uuid[])", [org, other_org])
        await conn.execute("DELETE FROM roles WHERE org_id = ANY($1::uuid[])", [org, other_org])
        await conn.execute("DELETE FROM user_files WHERE org_id = $1", org)
        await conn.execute("UPDATE users SET department_id = NULL WHERE org_id = $1", org)
        await conn.execute("DELETE FROM departments WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::uuid[])", [org, other_org])
    await pool.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ============================================
# files.py
# ============================================

@pytest.mark.asyncio(loop_scope="session")
async def test_list_files_scopes_to_current_user(setup):
    """GET /files WITHOUT actor user_id/org_id → returns ONLY the signer's own files.

    Identity comes from current_user (override = employee, non-admin). The route
    derives the owner from current_user["user_id"]; the admin's file must not leak.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/files")
    finally:
        clear_identity(app)
    assert r.status_code == 200, r.text
    ids = {f["id"] for f in r.json()}
    assert str(setup["emp_file"]) in ids
    assert str(setup["adm_file"]) not in ids


# ============================================
# calendar.py
# ============================================

@pytest.mark.asyncio(loop_scope="session")
async def test_list_events_scopes_to_current_user_org(setup):
    """GET /calendar/events WITHOUT actor org_id → scoped to current_user's org.

    The event in the OTHER org must not appear; the in-org event must.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/calendar/events")
    finally:
        clear_identity(app)
    assert r.status_code == 200, r.text
    ids = {e["id"] for e in r.json()}
    assert str(setup["my_event"]) in ids
    assert str(setup["other_event"]) not in ids


# ============================================
# departments.py
# ============================================

@pytest.mark.asyncio(loop_scope="session")
async def test_admin_sets_head_with_target_user_id(setup):
    """POST /departments/{id}/head/{target_user_id} as admin → target becomes head.

    Actor = admin (override). The target employee (already in the dept) gets
    is_head=true. Asserts via the live users row, since headship is stored on
    users.is_head (not on the departments row).
    """
    override_identity(app, user_id=setup["admin"], org_id=setup["org"], is_admin=True)
    try:
        async with _client() as c:
            r = await c.post(
                f"/api/v1/departments/{setup['dept']}/head/{setup['employee']}"
            )
    finally:
        clear_identity(app)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    async with setup["pool"].acquire() as conn:
        is_head = await conn.fetchval(
            "SELECT is_head FROM users WHERE id = $1", setup["employee"]
        )
    assert is_head is True


@pytest.mark.asyncio(loop_scope="session")
async def test_non_admin_cannot_set_head(setup):
    """POST /departments/{id}/head/{target_user_id} as non-admin → 403.

    Actor = employee (non-admin). _require_admin must reject before any mutation.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    try:
        async with _client() as c:
            r = await c.post(
                f"/api/v1/departments/{setup['dept']}/head/{setup['admin']}"
            )
    finally:
        clear_identity(app)
    assert r.status_code == 403, r.text
