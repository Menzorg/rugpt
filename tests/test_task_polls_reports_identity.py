"""Plan B Task 4: identity migration for routes/task_polls.py + task_reports.py.

Proves the poll/report route handlers derive identity from current_user
(device-signed, FAIL-CLOSED via get_current_user) and no longer accept actor
`user_id`/`org_id` as request params. Uses the lightweight `override_identity`
override (route logic depends only on current_user, not the signature path) and
inserts real rows via asyncpg so assertions verify actual behaviour, not just
status codes.

Coverage:
  - GET /task-polls/today      → returns the CURRENT user's poll, no user_id param
  - GET /task-polls (list)     → returns the CURRENT user's polls, no user_id param
  - GET /task-reports          → admin manager sees ONLY reports generated for them,
                                 no org_id/user_id params
"""
import os
import json
from datetime import date

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
    """Create org + manager (admin) + employee + outsider-org manager.

    Seed poll/report rows so the route assertions check real data:
      - employee has a pending poll today
      - manager (admin) has a report generated for them
      - another admin in the same org has a different report (manager must NOT see it)
    Cleanup wipes polls/reports/users/orgs in FK-safe order.
    """
    engine = engine_init
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (gen_random_uuid(), 'rt-polls', $1) RETURNING id",
            f"rt_polls_{uuid4().hex[:8]}",
        )
        manager = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'MGR', 'x', $3, true) RETURNING id",
            org, f"rt_polls_mgr_{uuid4().hex[:6]}",
            f"rt_polls_mgr_{uuid4()}@test.local",
        )
        employee = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email) "
            "VALUES (gen_random_uuid(), $1, $2, 'EMP', 'x', $3) RETURNING id",
            org, f"rt_polls_emp_{uuid4().hex[:6]}",
            f"rt_polls_emp_{uuid4()}@test.local",
        )
        other_admin = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_admin) "
            "VALUES (gen_random_uuid(), $1, $2, 'ADM2', 'x', $3, true) RETURNING id",
            org, f"rt_polls_adm2_{uuid4().hex[:6]}",
            f"rt_polls_adm2_{uuid4()}@test.local",
        )

        # Employee's pending poll for today
        emp_poll = await conn.fetchval(
            "INSERT INTO task_polls (id, org_id, assignee_user_id, poll_date, status) "
            "VALUES (gen_random_uuid(), $1, $2, $3, 'pending') RETURNING id",
            org, employee, date.today(),
        )
        # Manager's report (generated for the manager)
        mgr_report = await conn.fetchval(
            "INSERT INTO task_reports "
            "(id, org_id, generated_for_user_id, report_date, content, task_summaries) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5) RETURNING id",
            org, manager, date.today(), "MANAGER REPORT BODY",
            json.dumps([{"task": "t1"}]),
        )
        # A report for the OTHER admin — manager must never see this one
        other_report = await conn.fetchval(
            "INSERT INTO task_reports "
            "(id, org_id, generated_for_user_id, report_date, content, task_summaries) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, '[]'::jsonb) RETURNING id",
            org, other_admin, date.today(), "OTHER ADMIN REPORT",
        )
    yield {
        "engine": engine,
        "pool": pool,
        "org": org,
        "manager": manager,
        "employee": employee,
        "other_admin": other_admin,
        "emp_poll": emp_poll,
        "mgr_report": mgr_report,
        "other_report": other_report,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_reports WHERE org_id = $1", org)
        await conn.execute("DELETE FROM task_polls WHERE org_id = $1", org)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org)
    await pool.close()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="session")
async def test_today_poll_no_user_id_param(setup):
    """GET /task-polls/today WITHOUT user_id → returns the CURRENT user's poll.

    Identity comes from current_user (override_identity = employee). The route
    must resolve today's poll for that user without any actor param in the query.
    """
    override_identity(app, user_id=setup["employee"], org_id=setup["org"])
    try:
        async with _client() as c:
            r = await c.get("/api/v1/task-polls/today")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data is not None, "expected the employee's poll, got null"
        assert data["id"] == str(setup["emp_poll"])
        assert data["assignee_user_id"] == str(setup["employee"])
        assert data["status"] == "pending"
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_today_poll_is_per_current_user(setup):
    """The manager has no poll today → /today returns null for them.

    Confirms the result is scoped to current_user, not a request param: same
    endpoint, different identity, different (empty) result.
    """
    override_identity(app, user_id=setup["manager"], org_id=setup["org"], is_admin=True)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/task-polls/today")
        assert r.status_code == 200, r.text
        assert r.json() is None
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_list_polls_no_user_id_param(setup):
    """GET /task-polls WITHOUT user_id → lists the CURRENT user's pending polls."""
    override_identity(app, user_id=setup["employee"], org_id=setup["org"])
    try:
        async with _client() as c:
            r = await c.get("/api/v1/task-polls")
        assert r.status_code == 200, r.text
        polls = r.json()
        ids = {p["id"] for p in polls}
        assert str(setup["emp_poll"]) in ids
        assert all(p["assignee_user_id"] == str(setup["employee"]) for p in polls)
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_reports_no_org_or_user_param(setup):
    """GET /task-reports WITHOUT org_id/user_id → manager sees ONLY their report.

    The other admin's report (same org) must not leak — the route filters by
    current_user.user_id, not by any request param.
    """
    override_identity(app, user_id=setup["manager"], org_id=setup["org"], is_admin=True)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/task-reports")
        assert r.status_code == 200, r.text
        reports = r.json()
        ids = {rep["id"] for rep in reports}
        assert str(setup["mgr_report"]) in ids
        assert str(setup["other_report"]) not in ids, "leaked another admin's report"
        assert all(
            rep["generated_for_user_id"] == str(setup["manager"]) for rep in reports
        )
        mine = next(rep for rep in reports if rep["id"] == str(setup["mgr_report"]))
        assert mine["content"] == "MANAGER REPORT BODY"
    finally:
        clear_identity(app)


@pytest.mark.asyncio(loop_scope="session")
async def test_reports_non_admin_gets_empty(setup):
    """Non-admin employee → empty list regardless of any param (authz from current_user)."""
    override_identity(app, user_id=setup["employee"], org_id=setup["org"], is_admin=False)
    try:
        async with _client() as c:
            r = await c.get("/api/v1/task-reports")
        assert r.status_code == 200, r.text
        assert r.json() == []
    finally:
        clear_identity(app)
