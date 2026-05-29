"""TaskPollService.create_daily_poll — creates chat + enqueues poll_initial."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.services.task_poll_service import TaskPollService
from src.engine.models.chat import Chat, ChatType


def make_service(
    active_tasks=None,
    poll_interviewer_user_id=None,
    interviewer_lookup_returns=None,  # sentinel for "not found" tests
):
    storage = AsyncMock()
    storage.get_by_user_and_date = AsyncMock(return_value=None)  # no existing poll
    storage.create = AsyncMock(side_effect=lambda p: p)

    task_service = AsyncMock()
    task_service.storage = AsyncMock()
    task_service.storage.list_active_for_polls = AsyncMock(
        return_value=active_tasks or []
    )

    in_app = AsyncMock()
    in_app.create = AsyncMock()

    chat_service = AsyncMock()
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=uuid4())
    chat_service.create_poll_chat = AsyncMock(return_value=chat)

    ai_service = AsyncMock()
    ai_service.enqueue_poll_initial = AsyncMock(return_value=uuid4())

    user_storage = AsyncMock()
    if interviewer_lookup_returns is not None:
        # Caller wants explicit return (e.g. None for "not found" case).
        # Use a sentinel because default-None means "use mocked user".
        if interviewer_lookup_returns == "MISSING":
            user_storage.get_by_username = AsyncMock(return_value=None)
        else:
            user_storage.get_by_username = AsyncMock(
                return_value=interviewer_lookup_returns
            )
    else:
        user_storage.get_by_username = AsyncMock(
            return_value=MagicMock(id=poll_interviewer_user_id or uuid4())
        )

    service = TaskPollService(
        storage=storage,
        task_service=task_service,
        in_app_notification_service=in_app,
    )
    # Wire post-construction (matches engine_service.py pattern for circular deps)
    service.chat_service = chat_service
    service.ai_service = ai_service
    service.user_storage = user_storage

    return service, storage, chat_service, ai_service, in_app


@pytest.mark.asyncio
async def test_create_daily_poll_skips_when_no_active_tasks():
    service, storage, chat_svc, ai_svc, in_app = make_service(active_tasks=[])
    poll = await service.create_daily_poll(uuid4(), uuid4())
    assert poll is None
    storage.create.assert_not_awaited()
    chat_svc.create_poll_chat.assert_not_awaited()
    ai_svc.enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_daily_poll_creates_chat_and_enqueues():
    task = MagicMock(id=uuid4(), title="t1")
    service, storage, chat_svc, ai_svc, in_app = make_service(active_tasks=[task])

    poll = await service.create_daily_poll(uuid4(), uuid4())

    assert poll is not None
    assert [str(x) for x in poll.task_ids] == [str(task.id)]
    storage.create.assert_awaited_once()
    chat_svc.create_poll_chat.assert_awaited_once()
    ai_svc.enqueue_poll_initial.assert_awaited_once()
    in_app.create.assert_awaited_once()
