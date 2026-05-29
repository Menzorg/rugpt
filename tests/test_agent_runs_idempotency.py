"""
Tests for AgentRunStorage atomic CAS (pending -> running) and async AIService.

AgentRunStorage tests hit real Postgres — required for SQL atomicity.
"""
import asyncio
import pytest
import uuid as _uuid

from src.engine.config import Config
from src.engine.models.agent_run import AgentRun
from src.engine.storage.agent_run_storage import AgentRunStorage


DSN = Config.get_postgres_dsn()


def _run(coro):
    return asyncio.run(coro)


async def _make_storage() -> AgentRunStorage:
    storage = AgentRunStorage(DSN)
    await storage.init()
    return storage


async def _cleanup(storage: AgentRunStorage, request_id):
    await storage.execute("DELETE FROM agent_runs WHERE request_id = $1", request_id)


async def _fixture_chat_id(storage: AgentRunStorage):
    """Grab any existing chat id for FK satisfaction."""
    row = await storage.fetchrow("SELECT id FROM chats LIMIT 1")
    return row["id"] if row else None


def test_create_and_get():
    async def go():
        storage = await _make_storage()
        chat_id = await _fixture_chat_id(storage)
        if chat_id is None:
            pytest.skip("no chat fixture in dev DB")

        run = AgentRun(chat_id=chat_id, role_code="test", status="pending")
        created = await storage.create(run)
        assert created.request_id == run.request_id
        assert created.status == "pending"

        fetched = await storage.get(run.request_id)
        assert fetched is not None
        assert fetched.role_code == "test"

        await _cleanup(storage, run.request_id)
        await storage.close()

    _run(go())


def test_mark_running_cas_atomic_first_call_wins():
    async def go():
        storage = await _make_storage()
        chat_id = await _fixture_chat_id(storage)
        if chat_id is None:
            pytest.skip("no chat fixture in dev DB")

        run = AgentRun(chat_id=chat_id, role_code="test")
        await storage.create(run)

        # First mark_running wins
        first = await storage.mark_running(run.request_id)
        assert first is True

        # Second mark_running must return False (already running)
        second = await storage.mark_running(run.request_id)
        assert second is False

        # Verify status
        fetched = await storage.get(run.request_id)
        assert fetched.status == "running"
        assert fetched.started_at is not None

        await _cleanup(storage, run.request_id)
        await storage.close()

    _run(go())


def test_mark_running_does_not_retrigger_after_done():
    async def go():
        storage = await _make_storage()
        chat_id = await _fixture_chat_id(storage)
        if chat_id is None:
            pytest.skip("no chat fixture in dev DB")

        run = AgentRun(chat_id=chat_id, role_code="test")
        await storage.create(run)
        assert await storage.mark_running(run.request_id) is True

        # Simulate finished run
        fake_msg_id = _uuid.uuid4()
        await storage.fetchrow(
            "INSERT INTO messages (id, chat_id, sender_id, content, sender_type) "
            "VALUES ($1, $2, $2, 'x', 'ai_role') RETURNING id",
            fake_msg_id, chat_id,
        )
        await storage.mark_done(run.request_id, fake_msg_id)

        # Another mark_running must skip
        assert await storage.mark_running(run.request_id) is False
        fetched = await storage.get(run.request_id)
        assert fetched.status == "done"
        assert fetched.result_message_id == fake_msg_id
        assert fetched.finished_at is not None

        await storage.execute("DELETE FROM messages WHERE id = $1", fake_msg_id)
        await _cleanup(storage, run.request_id)
        await storage.close()

    _run(go())


def test_mark_failed_records_error():
    async def go():
        storage = await _make_storage()
        chat_id = await _fixture_chat_id(storage)
        if chat_id is None:
            pytest.skip("no chat fixture in dev DB")

        run = AgentRun(chat_id=chat_id, role_code="test")
        await storage.create(run)
        await storage.mark_running(run.request_id)

        await storage.mark_failed(run.request_id, "boom")
        fetched = await storage.get(run.request_id)
        assert fetched.status == "failed"
        assert fetched.error_message == "boom"

        await _cleanup(storage, run.request_id)
        await storage.close()

    _run(go())


def test_concurrent_mark_running_exactly_one_wins():
    """Simulate two consumers picking up the same Kafka message."""
    async def go():
        storage = await _make_storage()
        chat_id = await _fixture_chat_id(storage)
        if chat_id is None:
            pytest.skip("no chat fixture in dev DB")

        run = AgentRun(chat_id=chat_id, role_code="test")
        await storage.create(run)

        results = await asyncio.gather(
            storage.mark_running(run.request_id),
            storage.mark_running(run.request_id),
            storage.mark_running(run.request_id),
        )
        assert sum(1 for r in results if r) == 1, f"expected exactly one winner, got {results}"

        await _cleanup(storage, run.request_id)
        await storage.close()

    _run(go())
