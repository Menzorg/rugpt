"""SchedulerService bounded retry for stuck poll_initial generations."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.services.scheduler_service import SchedulerService
from src.engine.models.task_poll import TaskPoll
from src.engine.models.chat import Chat, ChatType


def make_scheduler(
    pending_polls=None,
    chat_for_poll=None,
    msg_count=0,
    failed_count=0,
    interviewer_user_id=None,
    has_user_msg=False,
    last_failed_at=None,
):
    task_poll_storage = AsyncMock()
    task_poll_storage.list_pending_today = AsyncMock(return_value=pending_polls or [])

    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(return_value=chat_for_poll)

    message_storage = AsyncMock()
    message_storage.count_by_chat = AsyncMock(return_value=msg_count)
    message_storage.create = AsyncMock()
    message_storage.messages_exist_from_sender = AsyncMock(return_value=has_user_msg)

    agent_run_storage = AsyncMock()
    agent_run_storage.count_failed_by_chat_and_kind = AsyncMock(return_value=failed_count)
    # Default: no recorded failure → cooldown gate is open. Tests that exercise
    # cooldown pass `last_failed_at=<recent datetime>` explicitly.
    agent_run_storage.last_failed_at = AsyncMock(return_value=last_failed_at)

    user_storage = AsyncMock()
    user_storage.get_by_username = AsyncMock(
        return_value=MagicMock(id=interviewer_user_id or uuid4()),
    )

    ai_service = AsyncMock()
    ai_service.enqueue_poll_initial = AsyncMock(return_value=uuid4())

    in_app = AsyncMock()
    in_app.create = AsyncMock()

    task_poll_service = AsyncMock()
    task_poll_service.storage = task_poll_storage

    sched = SchedulerService(
        calendar_service=AsyncMock(),
        notification_service=AsyncMock(),
        agent_executor=AsyncMock(),
        role_storage=AsyncMock(),
        user_storage=user_storage,
        org_storage=AsyncMock(),
        task_service=AsyncMock(),
        task_poll_service=task_poll_service,
        task_report_service=AsyncMock(),
    )
    # Wire poll-retry deps post-construction (engine_service.py pattern)
    sched.chat_storage = chat_storage
    sched.message_storage = message_storage
    sched.agent_run_storage = agent_run_storage
    sched.ai_service = ai_service
    sched.in_app_notification_service = in_app

    return sched, ai_service, message_storage, in_app


@pytest.mark.asyncio
async def test_retry_publishes_new_request_when_chat_empty_and_under_limit():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, msg_storage, in_app = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=0, failed_count=1,
    )

    await sched._retry_stuck_poll_initials()

    ai_service.enqueue_poll_initial.assert_awaited_once()
    msg_storage.create.assert_not_awaited()
    in_app.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_after_3_failures_writes_fallback_and_notifies():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, msg_storage, in_app = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=0, failed_count=3,
    )

    await sched._retry_stuck_poll_initials()

    ai_service.enqueue_poll_initial.assert_not_awaited()
    msg_storage.create.assert_awaited_once()
    in_app.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_retry_when_chat_has_messages():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    sched, ai_service, *_ = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat, msg_count=1, failed_count=0,
    )

    await sched._retry_stuck_poll_initials()

    ai_service.enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_when_no_pending_polls():
    sched, ai_service, *_ = make_scheduler(pending_polls=[])
    await sched._retry_stuck_poll_initials()
    ai_service.enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_when_chat_missing():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    sched, ai_service, *_ = make_scheduler(
        pending_polls=[poll], chat_for_poll=None,
    )
    await sched._retry_stuck_poll_initials()
    ai_service.enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_retry_during_cooldown_window():
    """failed_count=1 with last failure 30s ago → still in 5-min cooldown,
    must skip without re-enqueueing."""
    from datetime import datetime, timedelta
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    recent_failure = datetime.utcnow() - timedelta(seconds=30)
    sched, ai_service, *_ = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat,
        msg_count=0, failed_count=1, last_failed_at=recent_failure,
    )

    await sched._retry_stuck_poll_initials()

    ai_service.enqueue_poll_initial.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_cooldown_expired():
    """failed_count=1 with last failure 10 min ago → cooldown expired, retry."""
    from datetime import datetime, timedelta
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today(),
                    status="pending", org_id=uuid4())
    chat = Chat(id=uuid4(), type=ChatType.POLL, poll_id=poll.id)
    old_failure = datetime.utcnow() - timedelta(minutes=10)
    sched, ai_service, *_ = make_scheduler(
        pending_polls=[poll], chat_for_poll=chat,
        msg_count=0, failed_count=1, last_failed_at=old_failure,
    )

    await sched._retry_stuck_poll_initials()

    ai_service.enqueue_poll_initial.assert_awaited_once()
