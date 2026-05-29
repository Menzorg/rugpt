"""AIService.generate_poll_initial — generate AI greeting for poll chat."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import date
from uuid import uuid4

from src.engine.agents.result import AgentResult
from src.engine.models.task_poll import TaskPoll


def make_ai_service(
    poll=None,
    tasks_map=None,
    role=None,
    agent_result_content="Привет! Расскажи про задачи.",
):
    """Build an AIService with mocked deps for poll-initial unit tests."""
    from src.engine.services.ai_service import AIService

    poll_storage = AsyncMock()
    poll_storage.get_by_id = AsyncMock(return_value=poll)

    task_storage = AsyncMock()
    task_storage.get_many_by_ids = AsyncMock(return_value=tasks_map or {})

    role_storage = AsyncMock()
    role_storage.get_by_code = AsyncMock(return_value=role)

    user_storage = AsyncMock()
    user_storage.get_by_id = AsyncMock(return_value=MagicMock(name="Иван", username="ivan"))

    chat_storage = AsyncMock()
    message_storage = AsyncMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)

    agent_executor = AsyncMock()
    agent_executor.execute = AsyncMock(
        return_value=(AgentResult(content=agent_result_content), {})
    )

    agent_run_storage = AsyncMock()
    agent_run_storage.create = AsyncMock(side_effect=lambda r: r)

    kafka_producer = AsyncMock()
    kafka_producer.send = AsyncMock()

    service = AIService(
        role_storage=role_storage,
        user_storage=user_storage,
        chat_storage=chat_storage,
        message_storage=message_storage,
        agent_executor=agent_executor,
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer,
        task_poll_storage=poll_storage,
        task_storage=task_storage,
    )

    # Sanity-check: ctor wired the poll/task storages we passed in. Without these
    # asserts a future signature drift would silently break the tests' assumptions.
    assert service.task_poll_storage is poll_storage
    assert service.task_storage is task_storage

    return service, agent_executor, message_storage, poll_storage


@pytest.mark.asyncio
async def test_generate_poll_initial_persists_ai_message():
    poll_id = uuid4()
    chat_id = uuid4()
    responder_id = uuid4()
    assignee_id = uuid4()
    task_id = uuid4()

    poll = TaskPoll(
        id=poll_id,
        assignee_user_id=assignee_id,
        poll_date=date.today(),
        task_ids=[task_id],
    )
    task = MagicMock(id=task_id, title="Задача 1", deadline=None)
    role = MagicMock(code="poll_interviewer")

    service, executor, msg_storage, _ = make_ai_service(
        poll=poll, tasks_map={task_id: task}, role=role,
    )

    msg = await service.generate_poll_initial(poll_id, chat_id, responder_id)

    assert msg is not None
    executor.execute.assert_awaited_once()
    msg_storage.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_poll_initial_raises_when_role_missing():
    poll = TaskPoll(id=uuid4(), task_ids=[uuid4()])
    service, *_ = make_ai_service(poll=poll, role=None)

    with pytest.raises(RuntimeError, match="role.*not found"):
        await service.generate_poll_initial(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_initial_raises_when_poll_missing():
    service, *_ = make_ai_service(poll=None)

    with pytest.raises(RuntimeError, match="poll.*not found"):
        await service.generate_poll_initial(uuid4(), uuid4(), uuid4())


@pytest.mark.asyncio
async def test_generate_poll_initial_propagates_llm_error():
    poll = TaskPoll(id=uuid4(), task_ids=[])
    role = MagicMock(code="poll_interviewer")
    service, executor, *_ = make_ai_service(poll=poll, role=role)
    executor.execute = AsyncMock(side_effect=RuntimeError("llm down"))

    with pytest.raises(RuntimeError, match="llm down"):
        await service.generate_poll_initial(poll.id, uuid4(), uuid4())


@pytest.mark.asyncio
async def test_enqueue_poll_initial_publishes_kafka():
    poll = TaskPoll(id=uuid4())
    service, _, _, _ = make_ai_service(poll=poll, role=MagicMock())
    chat_id = uuid4()
    poll_id = uuid4()
    responder_id = uuid4()

    rid = await service.enqueue_poll_initial(poll_id, chat_id, responder_id)

    assert rid is not None
    service.kafka_producer.send.assert_awaited_once()
    args, kwargs = service.kafka_producer.send.call_args
    payload = args[1] if len(args) > 1 else (kwargs.get("value") or kwargs.get("payload"))
    assert payload["kind"] == "poll_initial"
    assert payload["poll_id"] == str(poll_id)
    assert payload["chat_id"] == str(chat_id)
