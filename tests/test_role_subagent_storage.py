"""Storage/service tests for supervisor subagent role mappings."""
import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.engine.config import Config
from src.engine.services.role_subagent_service import RoleSubagentService
from src.engine.storage.role_subagent_storage import RoleSubagentStorage


DSN = Config.get_postgres_dsn()


@pytest_asyncio.fixture
async def role_subagent_fixtures():
    pool = await asyncpg.create_pool(DSN)
    suffix = uuid4().hex[:8]
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            """
            INSERT INTO organizations (id, name, slug)
            VALUES (gen_random_uuid(), $1, $2)
            RETURNING id
            """,
            "Supervisor Test Org",
            f"supervisor-test-{suffix}",
        )
        cross_org_id = await conn.fetchval(
            """
            INSERT INTO organizations (id, name, slug)
            VALUES (gen_random_uuid(), $1, $2)
            RETURNING id
            """,
            "Supervisor Cross Org",
            f"supervisor-cross-{suffix}",
        )

        supervisor_id = await _insert_role(conn, org_id, "Supervisor", f"supervisor_{suffix}")
        active_id = await _insert_role(
            conn,
            org_id,
            "Alpha Active",
            f"active_{suffix}",
            as_subagent_description="Handles alpha work",
        )
        cross_active_id = await _insert_role(
            conn,
            cross_org_id,
            "Beta Cross Active",
            f"cross_active_{suffix}",
        )
        inactive_id = await _insert_role(
            conn,
            org_id,
            "Gamma Inactive",
            f"inactive_{suffix}",
            is_active=False,
        )

        await conn.execute(
            """
            INSERT INTO role_subagents (role_id, subagent_role_id)
            VALUES ($1, $2), ($1, $3), ($1, $4)
            """,
            supervisor_id,
            active_id,
            cross_active_id,
            inactive_id,
        )

    yield {
        "org_id": org_id,
        "cross_org_id": cross_org_id,
        "supervisor_id": supervisor_id,
        "active_id": active_id,
        "cross_active_id": cross_active_id,
        "inactive_id": inactive_id,
    }

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM organizations WHERE id = ANY($1::uuid[])", [org_id, cross_org_id])
    await pool.close()


async def _insert_role(conn, org_id, name, code, is_active=True, as_subagent_description=""):
    return await conn.fetchval(
        """
        INSERT INTO roles (
            id, org_id, name, code, description, system_prompt, model_name,
            agent_type, agent_config, tools, is_active, as_subagent_description
        )
        VALUES (
            gen_random_uuid(), $1, $2, $3, '', 'Prompt', 'qwen2.5:7b',
            'simple', $4::jsonb, $5::jsonb, $6, $7
        )
        RETURNING id
        """,
        org_id,
        name,
        code,
        json.dumps({}),
        json.dumps([]),
        is_active,
        as_subagent_description,
    )


@pytest.mark.asyncio
async def test_list_available_subagent_roles_filters_inactive_and_allows_cross_org(
    role_subagent_fixtures,
):
    storage = RoleSubagentStorage(DSN)
    await storage.init()
    try:
        roles = await storage.list_available_subagent_roles(
            role_subagent_fixtures["supervisor_id"],
        )
    finally:
        await storage.close()

    role_ids = [role.id for role in roles]
    assert role_ids == [
        role_subagent_fixtures["active_id"],
        role_subagent_fixtures["cross_active_id"],
    ]
    assert role_subagent_fixtures["inactive_id"] not in role_ids
    assert roles[1].org_id == role_subagent_fixtures["cross_org_id"]
    assert roles[0].as_subagent_description == "Handles alpha work"


@pytest.mark.asyncio
async def test_get_available_subagent_roles_returns_empty_for_unlinked_role(
    role_subagent_fixtures,
):
    storage = RoleSubagentStorage(DSN)
    await storage.init()
    service = RoleSubagentService(storage)
    try:
        roles = await service.get_available_subagent_roles(
            role_subagent_fixtures["active_id"],
        )
    finally:
        await storage.close()

    assert roles == []


@pytest.mark.asyncio
async def test_role_subagents_rejects_duplicate_links(role_subagent_fixtures):
    conn = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO role_subagents (role_id, subagent_role_id)
                VALUES ($1, $2)
                """,
                role_subagent_fixtures["supervisor_id"],
                role_subagent_fixtures["active_id"],
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_role_subagents_rejects_self_links(role_subagent_fixtures):
    conn = await asyncpg.connect(DSN)
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO role_subagents (role_id, subagent_role_id)
                VALUES ($1, $1)
                """,
                role_subagent_fixtures["supervisor_id"],
            )
    finally:
        await conn.close()
