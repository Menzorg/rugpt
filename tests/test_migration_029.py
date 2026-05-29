"""Smoke test for migration 029: poll chat type, poll_id FK, summary field, two roles, system user."""
import asyncpg
import pytest

from src.engine.config import Config


@pytest.mark.asyncio
async def test_migration_029_creates_schema_and_roles():
    conn = await asyncpg.connect(Config.get_postgres_dsn())
    try:
        # ChatType extension
        constraint = await conn.fetchval("""
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conname = 'chats_type_check'
        """)
        assert constraint and "'poll'" in constraint, f"chats_type_check missing 'poll': {constraint}"

        # chats.poll_id column + index
        col_exists = await conn.fetchval("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name='chats' AND column_name='poll_id'
        """)
        assert col_exists == 1

        idx_exists = await conn.fetchval("""
            SELECT 1 FROM pg_indexes
            WHERE indexname='idx_chats_poll'
        """)
        assert idx_exists == 1

        # task_polls.summary + task_ids
        for col in ("summary", "task_ids"):
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='task_polls' AND column_name=$1",
                col,
            )
            assert exists == 1, f"task_polls.{col} missing"

        # Two roles
        for code in ("poll_interviewer", "poll_summarizer"):
            role_exists = await conn.fetchval(
                "SELECT 1 FROM roles WHERE org_id='00000000-0000-0000-0000-000000000000'::uuid "
                "AND code=$1",
                code,
            )
            assert role_exists == 1, f"role {code} missing"

        # System user poll_interviewer_ai
        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE org_id='00000000-0000-0000-0000-000000000000'::uuid "
            "AND username='poll_interviewer_ai' AND is_system=true"
        )
        assert user_exists == 1, "system user poll_interviewer_ai missing"
    finally:
        await conn.close()
