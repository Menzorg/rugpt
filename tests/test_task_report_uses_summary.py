"""TaskReportService uses poll.summary in LLM input when present.

Three branches per assignee:
1. completed poll with summary → use summary
2. expired poll without summary but with user messages in chat → use raw transcript
3. expired poll without summary and no user messages → "опрос не пройден"
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date, datetime
from uuid import uuid4

from src.engine.models.task_poll import TaskPoll
from src.engine.models.message import Message, SenderType


def _make_message(content, sender_type=SenderType.USER, sender_id=None, chat_id=None):
    return Message(
        chat_id=chat_id or uuid4(),
        sender_id=sender_id or uuid4(),
        sender_type=sender_type,
        content=content,
    )


def _make_service(polls, chats_by_poll=None, messages_by_chat=None, agent_executor_result=None):
    """Build TaskReportService with mocked deps. agent_executor returns
    fixed content so we can verify input passed to LLM contains summary/transcript."""
    from src.engine.services.task_report_service import TaskReportService
    from src.engine.agents.result import AgentResult

    storage = AsyncMock()
    storage.create = AsyncMock(side_effect=lambda r: r)

    poll_service = AsyncMock()
    poll_service.list_by_org_and_date = AsyncMock(return_value=polls)

    in_app = AsyncMock()
    in_app.create = AsyncMock()

    role_storage = AsyncMock()
    role_storage.get_by_code = AsyncMock(return_value=MagicMock(code="report_generator"))

    chat_storage = AsyncMock()
    chat_storage.get_by_poll_id = AsyncMock(side_effect=lambda pid: (chats_by_poll or {}).get(pid))

    msg_storage = AsyncMock()
    msg_storage.list_by_chat = AsyncMock(side_effect=lambda cid, limit=500: (messages_by_chat or {}).get(cid, []))

    user_storage = AsyncMock()
    user_storage.get_by_id = AsyncMock(return_value=MagicMock(name="Иван", username="ivan"))

    task_storage = AsyncMock()
    task_storage.get_many_by_ids = AsyncMock(return_value={})

    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=agent_executor_result or AgentResult(content="## Generated report"))

    # Build service — adapt to real ctor signature
    service = TaskReportService(
        storage=storage,
        task_poll_service=poll_service,
        in_app_notification_service=in_app,
        role_storage=role_storage,
        task_storage=task_storage,
        user_storage=user_storage,
    )
    service.agent_executor = executor
    # Wire chat/message storage if needed for expired-with-transcript branch
    service.chat_storage = chat_storage
    service.message_storage = msg_storage
    return service, executor


@pytest.mark.asyncio
async def test_completed_poll_uses_summary_in_input():
    poll = TaskPoll(
        id=uuid4(), org_id=uuid4(), assignee_user_id=uuid4(),
        poll_date=date.today(), status="completed",
        summary="## По задачам\n- Task 1: в работе.\n- Task 2: готова.",
    )
    service, executor = _make_service(polls=[poll])

    report = await service.generate_report(
        org_id=poll.org_id,
        manager_user_id=uuid4(),
        report_date=date.today(),
    )

    assert report is not None
    executor.execute.assert_awaited_once()
    user_input = executor.execute.call_args.kwargs["messages"][0]["content"]
    assert "Task 1: в работе" in user_input or "По задачам" in user_input


@pytest.mark.asyncio
async def test_expired_poll_with_messages_uses_transcript():
    chat_id = uuid4()
    user_id = uuid4()
    poll = TaskPoll(
        id=uuid4(), org_id=uuid4(), assignee_user_id=user_id,
        poll_date=date.today(), status="expired",
        summary=None,
    )
    chat = MagicMock(id=chat_id, poll_id=poll.id)
    messages = [
        _make_message("Задача 1 в работе, проблем нет.", sender_type=SenderType.USER, sender_id=user_id, chat_id=chat_id),
    ]
    service, executor = _make_service(
        polls=[poll],
        chats_by_poll={poll.id: chat},
        messages_by_chat={chat_id: messages},
    )

    report = await service.generate_report(
        org_id=poll.org_id,
        manager_user_id=uuid4(),
        report_date=date.today(),
    )

    assert report is not None
    user_input = executor.execute.call_args.kwargs["messages"][0]["content"]
    assert "Задача 1 в работе" in user_input


@pytest.mark.asyncio
async def test_expired_poll_no_messages_uses_not_passed_marker():
    poll = TaskPoll(
        id=uuid4(), org_id=uuid4(), assignee_user_id=uuid4(),
        poll_date=date.today(), status="expired",
        summary=None,
    )
    service, executor = _make_service(polls=[poll], chats_by_poll={}, messages_by_chat={})

    report = await service.generate_report(
        org_id=poll.org_id,
        manager_user_id=uuid4(),
        report_date=date.today(),
    )

    assert report is not None
    user_input = executor.execute.call_args.kwargs["messages"][0]["content"]
    assert "не прошёл" in user_input.lower() or "опрос не пройден" in user_input.lower()


@pytest.mark.asyncio
async def test_mixed_polls_render_each_branch_per_employee():
    """Production scenario: one report covers 3 employees with different
    poll states. Verify each gets its correct branch in the LLM input.
    """
    org_id = uuid4()

    # Employee 1: completed with summary
    user1, chat1 = uuid4(), uuid4()
    poll1 = TaskPoll(
        id=uuid4(), org_id=org_id, assignee_user_id=user1,
        poll_date=date.today(), status="completed",
        summary="## По задачам\n- Task A: готова.",
    )

    # Employee 2: expired with diaog
    user2, chat2_id = uuid4(), uuid4()
    poll2 = TaskPoll(
        id=uuid4(), org_id=org_id, assignee_user_id=user2,
        poll_date=date.today(), status="expired",
        summary=None,
    )
    chat2 = MagicMock(id=chat2_id, poll_id=poll2.id)
    msgs2 = [
        _make_message("Я задержался с поставщиком.", sender_type=SenderType.USER, sender_id=user2, chat_id=chat2_id),
    ]

    # Employee 3: expired without dialog
    user3 = uuid4()
    poll3 = TaskPoll(
        id=uuid4(), org_id=org_id, assignee_user_id=user3,
        poll_date=date.today(), status="expired",
        summary=None,
    )

    service, executor = _make_service(
        polls=[poll1, poll2, poll3],
        chats_by_poll={poll2.id: chat2},
        messages_by_chat={chat2_id: msgs2},
    )

    report = await service.generate_report(
        org_id=org_id, manager_user_id=uuid4(), report_date=date.today(),
    )

    assert report is not None
    user_input = executor.execute.call_args.kwargs["messages"][0]["content"]
    # All three branches must be present in one input.
    assert "Task A: готова" in user_input
    assert "Я задержался с поставщиком" in user_input
    assert "не пройден" in user_input.lower()


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_plain_text_with_transcript():
    """When LLM returns empty, _fallback_plain_text takes over and must still
    render expired-poll transcripts (this branch has no test on the AI-path side
    because AI is mocked to succeed)."""
    from src.engine.agents.result import AgentResult

    user_id, chat_id = uuid4(), uuid4()
    poll = TaskPoll(
        id=uuid4(), org_id=uuid4(), assignee_user_id=user_id,
        poll_date=date.today(), status="expired",
        summary=None,
    )
    chat = MagicMock(id=chat_id, poll_id=poll.id)
    msgs = [
        _make_message("Передал заказ Иванову, статус: в работе.",
                      sender_type=SenderType.USER, sender_id=user_id, chat_id=chat_id),
    ]

    # LLM returns empty — _generate_ai_content returns None — _fallback fires.
    service, executor = _make_service(
        polls=[poll],
        chats_by_poll={poll.id: chat},
        messages_by_chat={chat_id: msgs},
        agent_executor_result=AgentResult(content=""),
    )

    report = await service.generate_report(
        org_id=poll.org_id, manager_user_id=uuid4(), report_date=date.today(),
    )

    assert report is not None
    # _fallback_plain_text path must include the transcript verbatim.
    assert "Передал заказ Иванову, статус: в работе" in report.content
