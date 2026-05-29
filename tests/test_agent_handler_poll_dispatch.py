"""AgentRequestHandler dispatch by 'kind' payload field."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.kafka.agent_handler import AgentRequestHandler


def make_handler():
    ai_service = AsyncMock()
    ai_service.generate_response = AsyncMock(
        return_value=MagicMock(id=uuid4(), to_dict=lambda: {})
    )
    ai_service.generate_poll_initial = AsyncMock(
        return_value=MagicMock(id=uuid4(), to_dict=lambda: {})
    )
    ai_service.generate_poll_summary = AsyncMock(
        return_value=MagicMock(id=uuid4(), to_dict=lambda: {})
    )

    msg_storage = AsyncMock()
    msg_storage.get_by_id = AsyncMock(return_value=MagicMock(id=uuid4()))

    agent_run_storage = AsyncMock()
    agent_run_storage.mark_running = AsyncMock(return_value=True)
    agent_run_storage.mark_done = AsyncMock()
    agent_run_storage.mark_failed = AsyncMock()
    agent_run_storage.get = AsyncMock()

    kafka = AsyncMock()
    kafka.send = AsyncMock()

    return AgentRequestHandler(
        ai_service=ai_service,
        message_storage=msg_storage,
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka,
    ), ai_service


@pytest.mark.asyncio
async def test_dispatch_message_reply_calls_generate_response():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "user_message_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "message_reply",
    }
    await handler(payload)
    ai.generate_response.assert_awaited_once()
    ai.generate_poll_initial.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_default_kind_is_message_reply():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "user_message_id": str(uuid4()),
        "responder_id": str(uuid4()),
        # no 'kind' — must default to message_reply
    }
    await handler(payload)
    ai.generate_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_poll_initial_calls_generate_poll_initial():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_initial",
        "poll_id": str(uuid4()),
    }
    await handler(payload)
    ai.generate_poll_initial.assert_awaited_once()
    ai.generate_response.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_poll_summary_calls_generate_poll_summary():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_summary",
        "poll_id": str(uuid4()),
    }
    await handler(payload)
    ai.generate_poll_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_kind_skips_silently():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "made_up_kind",
    }
    # Should NOT raise (poison message protection)
    await handler(payload)
    ai.generate_response.assert_not_awaited()
    ai.generate_poll_initial.assert_not_awaited()
    ai.generate_poll_summary.assert_not_awaited()
    # CAS lock must NOT be acquired for poison messages — otherwise we'd
    # "burn" an agent_run and Kafka redelivery would skip silently.
    handler.agent_run_storage.mark_running.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_poll_initial_missing_poll_id_skips():
    handler, ai = make_handler()
    payload = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "kind": "poll_initial",
        # poll_id missing
    }
    await handler(payload)
    ai.generate_poll_initial.assert_not_awaited()
    # CAS lock must NOT be acquired — validation must short-circuit before mark_running.
    handler.agent_run_storage.mark_running.assert_not_awaited()
