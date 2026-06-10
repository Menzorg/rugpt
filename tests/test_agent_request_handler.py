"""
Tests for AgentRequestHandler — the Kafka consumer handler that executes
async agent runs with idempotency guarantees.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.engine.kafka.agent_handler import AgentRequestHandler


def make_handler(mark_running_result: bool = True, generate_result=None, raise_on_generate=False):
    ai_service = AsyncMock()
    if raise_on_generate:
        ai_service.generate_response = AsyncMock(side_effect=RuntimeError("llm down"))
    else:
        ai_service.generate_response = AsyncMock(return_value=generate_result)

    message_storage = AsyncMock()
    message_storage.get_by_id = AsyncMock(return_value=MagicMock(id=uuid4()))

    agent_run_storage = AsyncMock()
    agent_run_storage.mark_running = AsyncMock(return_value=mark_running_result)
    agent_run_storage.mark_done = AsyncMock()
    agent_run_storage.mark_failed = AsyncMock()
    agent_run_storage.get = AsyncMock(return_value=MagicMock(status="done"))

    kafka_producer = AsyncMock()
    kafka_producer.send = AsyncMock()

    handler = AgentRequestHandler(
        ai_service=ai_service,
        message_storage=message_storage,
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer,
    )
    return handler, ai_service, message_storage, agent_run_storage, kafka_producer


def _payload(**overrides):
    base = {
        "request_id": str(uuid4()),
        "chat_id": str(uuid4()),
        "user_message_id": str(uuid4()),
        "triggering_user_id": str(uuid4()),
        "responder_id": str(uuid4()),
        "strip_username": None,
        "role_code": "",
    }
    base.update(overrides)
    return base


def test_happy_path_marks_done_and_publishes():
    async def go():
        ai_msg = MagicMock()
        ai_msg.id = uuid4()
        ai_msg.to_dict = MagicMock(return_value={"id": str(ai_msg.id), "content": "reply"})

        handler, ai_service, message_storage, agent_run_storage, kafka_producer = make_handler(
            mark_running_result=True, generate_result=ai_msg,
        )

        payload = _payload()
        await handler(payload)

        agent_run_storage.mark_running.assert_called_once()
        ai_service.generate_response.assert_called_once()
        agent_run_storage.mark_done.assert_called_once()
        kafka_producer.send.assert_called_once()
        call = kafka_producer.send.call_args
        assert call.args[0] == "chat.events"
        assert call.args[1]["chat_id"] == payload["chat_id"]

    asyncio.run(go())


def test_message_reply_forwards_invocation_kind_override():
    async def go():
        ai_msg = MagicMock()
        ai_msg.id = uuid4()
        ai_msg.to_dict = MagicMock(return_value={"id": str(ai_msg.id), "content": "reply"})

        handler, ai_service, _, _, _ = make_handler(
            mark_running_result=True, generate_result=ai_msg,
        )

        payload = _payload(invocation_kind_override="mention")
        await handler(payload)

        call = ai_service.generate_response.call_args
        assert call.kwargs["invocation_kind_override"] == "mention"

    asyncio.run(go())


def test_message_reply_records_support_first_response():
    """Regression guard: the async handler must stamp support-ticket SLA after a
    successful message_reply (this used to live only in the now-removed sync path,
    so prod silently never stamped ai_first_response_at)."""
    async def go():
        ai_msg = MagicMock()
        ai_msg.id = uuid4()
        ai_msg.to_dict = MagicMock(return_value={"id": str(ai_msg.id), "content": "reply"})

        handler, ai_service, _, _, _ = make_handler(
            mark_running_result=True, generate_result=ai_msg,
        )

        payload = _payload()
        await handler(payload)

        ai_service.record_support_first_response.assert_awaited_once()
        args = ai_service.record_support_first_response.call_args.args
        assert args[0] == UUID(payload["chat_id"])
        assert args[1] == UUID(payload["responder_id"])
        assert args[2] == ai_msg.id

    asyncio.run(go())


def test_no_support_stamp_when_generate_returns_none():
    """If no AI message was produced, the handler must not attempt the SLA stamp."""
    async def go():
        handler, ai_service, _, _, _ = make_handler(
            mark_running_result=True, generate_result=None,
        )

        await handler(_payload())

        ai_service.record_support_first_response.assert_not_called()

    asyncio.run(go())


def test_already_handled_is_skipped_silently():
    async def go():
        handler, ai_service, _, agent_run_storage, kafka_producer = make_handler(
            mark_running_result=False,
        )

        payload = _payload()
        await handler(payload)

        ai_service.generate_response.assert_not_called()
        agent_run_storage.mark_done.assert_not_called()
        kafka_producer.send.assert_not_called()

    asyncio.run(go())


def test_generate_returns_none_marks_failed_without_raise():
    async def go():
        handler, ai_service, _, agent_run_storage, kafka_producer = make_handler(
            mark_running_result=True, generate_result=None,
        )

        payload = _payload()
        # Must NOT raise — returning None is a valid failure mode
        await handler(payload)

        agent_run_storage.mark_failed.assert_called_once()
        agent_run_storage.mark_done.assert_not_called()
        kafka_producer.send.assert_not_called()

    asyncio.run(go())


def test_generate_raises_marks_failed_and_reraises():
    async def go():
        handler, _, _, agent_run_storage, _ = make_handler(
            mark_running_result=True, raise_on_generate=True,
        )

        payload = _payload()
        with pytest.raises(RuntimeError):
            await handler(payload)

        agent_run_storage.mark_failed.assert_called_once()
        args = agent_run_storage.mark_failed.call_args.args
        assert "llm down" in args[1]

    asyncio.run(go())


def test_malformed_payload_returns_without_raise():
    async def go():
        handler, ai_service, _, _, _ = make_handler()

        await handler({"missing": "keys"})

        ai_service.generate_response.assert_not_called()

    asyncio.run(go())


def test_missing_user_message_marks_failed():
    async def go():
        handler, _, message_storage, agent_run_storage, _ = make_handler(
            mark_running_result=True,
        )
        message_storage.get_by_id = AsyncMock(return_value=None)

        payload = _payload()
        with pytest.raises(RuntimeError):
            await handler(payload)

        agent_run_storage.mark_failed.assert_called_once()

    asyncio.run(go())
