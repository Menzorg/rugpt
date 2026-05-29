"""Storage roundtrip tests for new poll-related fields (Task 3 of poll-ai-dialog plan).

Hits real Postgres (rugpt DB) to verify SELECT/INSERT round-trips for:
  - chats.poll_id
  - task_polls.summary, task_polls.task_ids (JSONB)
  - new helpers: ChatStorage.get_by_poll_id, TaskPollStorage.update_summary
"""
import json
import pytest
from datetime import date
from uuid import UUID, uuid4

from src.engine.config import Config
from src.engine.models.chat import Chat, ChatType
from src.engine.models.task_poll import TaskPoll
from src.engine.storage.chat_storage import ChatStorage
from src.engine.storage.task_poll_storage import TaskPollStorage


SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")


async def _make_chat_storage() -> ChatStorage:
    storage = ChatStorage(Config.get_postgres_dsn())
    await storage.init()
    return storage


async def _make_poll_storage() -> TaskPollStorage:
    storage = TaskPollStorage(Config.get_postgres_dsn())
    await storage.init()
    return storage


async def _grab_user_id(storage) -> UUID:
    """Grab any active user from the system org for FK satisfaction.

    Migration 029 creates `poll_interviewer_ai`; otherwise any active user
    in the system org is fine for these storage-level tests.
    """
    row = await storage.fetchrow(
        "SELECT id FROM users WHERE org_id = $1 AND is_active = true LIMIT 1",
        SYSTEM_ORG_ID,
    )
    if row is None:
        pytest.skip("no active user in system org for FK satisfaction")
    return row["id"]


async def _cleanup_chat(storage, chat_id):
    await storage.execute("DELETE FROM chats WHERE id = $1", chat_id)


async def _cleanup_poll(storage, poll_id):
    # chat → poll FK is ON DELETE CASCADE, but tests delete chat first anyway
    await storage.execute("DELETE FROM task_polls WHERE id = $1", poll_id)


@pytest.mark.asyncio
async def test_chat_storage_roundtrips_poll_id():
    chat_storage = await _make_chat_storage()
    poll_storage = await _make_poll_storage()
    try:
        user_id = await _grab_user_id(poll_storage)

        poll = TaskPoll(
            org_id=SYSTEM_ORG_ID,
            assignee_user_id=user_id,
            poll_date=date.today(),
            task_ids=[uuid4()],
        )
        created_poll = await poll_storage.create(poll)

        chat = Chat(
            org_id=SYSTEM_ORG_ID,
            type=ChatType.POLL,
            participants=[user_id],
            poll_id=created_poll.id,
        )
        created_chat = await chat_storage.create(chat)
        assert created_chat.poll_id == created_poll.id

        fetched = await chat_storage.get_by_id(created_chat.id)
        assert fetched is not None
        assert fetched.poll_id == created_poll.id
        assert fetched.type == ChatType.POLL

        await _cleanup_chat(chat_storage, created_chat.id)
        await _cleanup_poll(poll_storage, created_poll.id)
    finally:
        await chat_storage.close()
        await poll_storage.close()


@pytest.mark.asyncio
async def test_chat_storage_get_by_poll_id():
    chat_storage = await _make_chat_storage()
    poll_storage = await _make_poll_storage()
    try:
        user_id = await _grab_user_id(poll_storage)

        poll = TaskPoll(
            org_id=SYSTEM_ORG_ID,
            assignee_user_id=user_id,
            poll_date=date.today(),
        )
        created_poll = await poll_storage.create(poll)

        chat = Chat(
            org_id=SYSTEM_ORG_ID,
            type=ChatType.POLL,
            participants=[user_id],
            poll_id=created_poll.id,
        )
        created_chat = await chat_storage.create(chat)

        found = await chat_storage.get_by_poll_id(created_poll.id)
        assert found is not None
        assert found.id == created_chat.id

        await _cleanup_chat(chat_storage, created_chat.id)
        await _cleanup_poll(poll_storage, created_poll.id)
    finally:
        await chat_storage.close()
        await poll_storage.close()


@pytest.mark.asyncio
async def test_chat_storage_get_by_poll_id_returns_none_for_missing():
    chat_storage = await _make_chat_storage()
    try:
        result = await chat_storage.get_by_poll_id(uuid4())
        assert result is None
    finally:
        await chat_storage.close()


@pytest.mark.asyncio
async def test_task_poll_storage_roundtrips_summary_and_task_ids():
    poll_storage = await _make_poll_storage()
    try:
        user_id = await _grab_user_id(poll_storage)
        task_ids = [uuid4(), uuid4()]

        poll = TaskPoll(
            org_id=SYSTEM_ORG_ID,
            assignee_user_id=user_id,
            poll_date=date.today(),
            task_ids=task_ids,
        )
        created = await poll_storage.create(poll)
        assert [str(x) for x in created.task_ids] == [str(x) for x in task_ids]

        fetched = await poll_storage.get_by_id(created.id)
        assert fetched is not None
        assert [str(x) for x in fetched.task_ids] == [str(x) for x in task_ids]
        assert fetched.summary is None

        await _cleanup_poll(poll_storage, created.id)
    finally:
        await poll_storage.close()


@pytest.mark.asyncio
async def test_task_poll_storage_update_summary():
    poll_storage = await _make_poll_storage()
    try:
        user_id = await _grab_user_id(poll_storage)
        poll = TaskPoll(
            org_id=SYSTEM_ORG_ID,
            assignee_user_id=user_id,
            poll_date=date.today(),
        )
        created = await poll_storage.create(poll)

        await poll_storage.update_summary(created.id, "## test summary")
        fetched = await poll_storage.get_by_id(created.id)
        assert fetched is not None
        assert fetched.summary == "## test summary"
        # update_summary must NOT touch status
        assert fetched.status == created.status

        await _cleanup_poll(poll_storage, created.id)
    finally:
        await poll_storage.close()
