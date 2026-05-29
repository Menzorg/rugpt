"""AIService.generate_poll_summary — final summary at submit time."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.agents.result import AgentResult
from src.engine.models.task_poll import TaskPoll


def make_service(
    poll=None,
    messages=None,
    role=None,
    agent_result_content="## По задачам\n- Задача 1: в работе.",
):
    """Build AIService with mocked deps for poll_summary unit tests.

    Match real AIService.__init__ signature (no try/except fallback —
    we want signature drift to fail loudly).
    """
    from src.engine.services.ai_service import AIService

    poll_storage = AsyncMock()
    poll_storage.get_by_id = AsyncMock(return_value=poll)
    poll_storage.update_summary = AsyncMock()
    poll_storage.update_status = AsyncMock()

    msg_storage = AsyncMock()
    msg_storage.list_by_chat = AsyncMock(return_value=messages or [])
    msg_storage.create = AsyncMock(side_effect=lambda m: m)

    role_storage = AsyncMock()
    role_storage.get_by_code = AsyncMock(return_value=role)

    executor = AsyncMock()
    executor.execute = AsyncMock(
        return_value=(AgentResult(content=agent_result_content), {})
    )

    user_storage = AsyncMock()
    chat_storage = AsyncMock()
    task_storage = AsyncMock()
    agent_run_storage = AsyncMock()
    agent_run_storage.create = AsyncMock(side_effect=lambda r: r)
    kafka_producer = AsyncMock()
    kafka_producer.send = AsyncMock()

    # Match real AIService signature (adapt if drifted)
    service = AIService(
        chat_storage=chat_storage,
        message_storage=msg_storage,
        user_storage=user_storage,
        role_storage=role_storage,
        agent_executor=executor,
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer,
        task_poll_storage=poll_storage,
        task_storage=task_storage,
    )
    assert service.task_poll_storage is poll_storage
    return service, executor, poll_storage, msg_storage


@pytest.mark.asyncio
async def test_generate_poll_summary_writes_summary_and_completes_poll():
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today())
    role = MagicMock(code="poll_summarizer")
    user_msg = MagicMock(sender_type="user", content="Задача 1 в работе", sender_id=poll.assignee_user_id)
    ai_msg = MagicMock(sender_type="ai_role", content="Понял.", sender_id=uuid4())

    service, executor, poll_storage, msg_storage = make_service(
        poll=poll, messages=[user_msg, ai_msg], role=role,
    )

    chat_id = uuid4()
    responder_id = uuid4()
    sys_msg = await service.generate_poll_summary(poll.id, chat_id, responder_id)

    assert sys_msg is not None
    poll_storage.update_summary.assert_awaited_once()
    args = poll_storage.update_summary.call_args[0]
    assert args[0] == poll.id
    assert "По задачам" in args[1]

    poll_storage.update_status.assert_awaited_once()
    # Persisted system "Отчёт сдан" message
    assert msg_storage.create.await_count == 1


@pytest.mark.asyncio
async def test_generate_poll_summary_raises_on_empty_llm_output():
    poll = TaskPoll(id=uuid4())
    role = MagicMock(code="poll_summarizer")
    service, executor, *_ = make_service(poll=poll, messages=[MagicMock()], role=role,
                                          agent_result_content="")

    with pytest.raises(RuntimeError, match="empty"):
        await service.generate_poll_summary(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_summary_raises_when_role_missing():
    poll = TaskPoll(id=uuid4())
    service, *_ = make_service(poll=poll, messages=[MagicMock()], role=None)

    with pytest.raises(RuntimeError, match="role.*not found"):
        await service.generate_poll_summary(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_enqueue_poll_summary_publishes_kafka():
    poll = TaskPoll(id=uuid4())
    service, _, _, _ = make_service(poll=poll, role=MagicMock())
    rid = await service.enqueue_poll_summary(poll.id, uuid4(), uuid4())
    assert rid is not None
    service.kafka_producer.send.assert_awaited_once()
    args = service.kafka_producer.send.call_args[0]
    payload = args[1]
    assert payload["kind"] == "poll_summary"
    assert payload["poll_id"] == str(poll.id)


@pytest.mark.asyncio
async def test_generate_poll_summary_empty_transcript_passes_marker_to_llm():
    """Empty chat — transcript should fall back to '(пусто)' and still call LLM
    (which will, per prompt instructions, produce 'Сотрудник не предоставил информации')."""
    poll = TaskPoll(id=uuid4(), assignee_user_id=uuid4(), poll_date=date.today())
    role = MagicMock(code="poll_summarizer")
    service, executor, poll_storage, msg_storage = make_service(
        poll=poll, messages=[], role=role,
    )

    sys_msg = await service.generate_poll_summary(poll.id, uuid4(), uuid4())

    assert sys_msg is not None
    executor.execute.assert_awaited_once()
    call_kwargs = executor.execute.call_args.kwargs
    user_input = call_kwargs["messages"][0]["content"]
    assert "(пусто)" in user_input
