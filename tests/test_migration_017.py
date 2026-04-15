"""
Schema checks for migration 017.

Requires the dev PostgreSQL database at localhost/rugpt. The test is
skipped gracefully if the connection cannot be established.
"""
import asyncio
import pytest

import asyncpg


DSN = "postgresql://postgres@localhost/rugpt"


_SKIP = object()


async def _fetchval_async(sql, *args):
    try:
        conn = await asyncpg.connect(DSN)
    except Exception:
        return _SKIP
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


def _fetchval(sql, *args):
    result = asyncio.run(_fetchval_async(sql, *args))
    if result is _SKIP:
        pytest.skip("dev postgres not available")
    return result


def test_projects_table_exists():
    exists = _fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name='projects')"
    )
    assert exists is True


def test_task_events_table_exists():
    exists = _fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name='task_events')"
    )
    assert exists is True


def test_tasks_project_id_column_added_nullable():
    row = _fetchval(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='tasks' AND column_name='project_id'"
    )
    assert row == "YES"


def test_chats_has_task_id_column():
    col = _fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='chats' AND column_name='task_id'"
    )
    assert col == "task_id"


def test_chats_has_project_id_column():
    col = _fetchval(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='chats' AND column_name='project_id'"
    )
    assert col == "project_id"


def test_chats_type_default_is_direct():
    default = _fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name='chats' AND column_name='type'"
    )
    assert default is not None and "direct" in default


def test_legacy_main_group_types_converted():
    remaining = _fetchval(
        "SELECT COUNT(*) FROM chats WHERE type IN ('main','group')"
    )
    assert remaining == 0


def test_idx_chats_task_exists():
    name = _fetchval(
        "SELECT indexname FROM pg_indexes WHERE indexname='idx_chats_task'"
    )
    assert name == "idx_chats_task"


def test_idx_chats_project_exists():
    name = _fetchval(
        "SELECT indexname FROM pg_indexes WHERE indexname='idx_chats_project'"
    )
    assert name == "idx_chats_project"
