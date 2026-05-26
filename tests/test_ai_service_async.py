"""
Tests for AIService async mode: process_ai_mentions + try_auto_respond
should enqueue via Kafka + agent_runs instead of running synchronously.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.agents.result import AgentResult
from src.engine.models.chat import Chat, ChatType
from src.engine.models.message import Message, Mention, MentionType, SenderType
from src.engine.models.role import Role
from src.engine.models.user import User
from src.engine.services.ai_service import AIService


def make_message(chat_id=None, sender_id=None, mentions=None):
    return Message(
        id=uuid4(),
        chat_id=chat_id or uuid4(),
        sender_id=sender_id or uuid4(),
        sender_type=SenderType.USER,
        content="hello",
        mentions=mentions or [],
    )


def make_ai_service(async_mode: bool = True) -> AIService:
    kafka_producer = MagicMock()
    kafka_producer.enabled = async_mode
    kafka_producer.send = AsyncMock()

    agent_run_storage = AsyncMock()
    agent_run_storage.create = AsyncMock(side_effect=lambda r: r)

    svc = AIService(
        role_storage=AsyncMock(),
        user_storage=AsyncMock(),
        chat_storage=AsyncMock(),
        message_storage=AsyncMock(),
        agent_run_storage=agent_run_storage,
        kafka_producer=kafka_producer if async_mode else None,
    )
    return svc


def test_is_async_mode_true_when_wired():
    svc = make_ai_service(async_mode=True)
    assert svc._is_async_mode() is True


def test_is_async_mode_false_without_producer():
    svc = make_ai_service(async_mode=False)
    assert svc._is_async_mode() is False


def test_process_ai_mentions_async_enqueues():
    async def go():
        svc = make_ai_service(async_mode=True)
        ai_mention = Mention(
            type=MentionType.AI_ROLE,
            user_id=uuid4(),
            username="qwen3",
            position=0,
        )
        msg = make_message(mentions=[ai_mention])

        result = await svc.process_ai_mentions(msg, org_id=uuid4())

        # Async mode returns empty list
        assert result == []
        # Agent run created + Kafka publish
        svc.agent_run_storage.create.assert_called_once()
        svc.kafka_producer.send.assert_called_once()
        call = svc.kafka_producer.send.call_args
        assert call.args[0] == "agent.requests"
        assert call.args[1]["user_message_id"] == str(msg.id)
        assert call.args[1]["responder_id"] == str(ai_mention.user_id)
        assert call.args[1]["strip_username"] == "qwen3"

    asyncio.run(go())


def test_process_ai_mentions_no_mentions_returns_empty_and_no_enqueue():
    async def go():
        svc = make_ai_service(async_mode=True)
        msg = make_message(mentions=[])

        result = await svc.process_ai_mentions(msg, org_id=uuid4())

        assert result == []
        svc.agent_run_storage.create.assert_not_called()
        svc.kafka_producer.send.assert_not_called()

    asyncio.run(go())


def test_process_ai_mentions_multiple_enqueues_each():
    async def go():
        svc = make_ai_service(async_mode=True)
        mentions = [
            Mention(type=MentionType.AI_ROLE, user_id=uuid4(), username=f"role{i}", position=i)
            for i in range(3)
        ]
        msg = make_message(mentions=mentions)

        await svc.process_ai_mentions(msg, org_id=uuid4())

        assert svc.agent_run_storage.create.call_count == 3
        assert svc.kafka_producer.send.call_count == 3

    asyncio.run(go())


def test_try_auto_respond_async_enqueues_when_system_user_present():
    async def go():
        svc = make_ai_service(async_mode=True)
        sender = uuid4()
        system_user_id = uuid4()

        chat = Chat(
            org_id=uuid4(),
            type=ChatType.DIRECT,
            participants=[sender, system_user_id],
        )
        svc.chat_storage.get_by_id = AsyncMock(return_value=chat)

        async def fake_get_user(uid):
            user = MagicMock()
            user.id = uid
            user.is_system = uid == system_user_id
            return user

        svc.user_storage.get_by_id = AsyncMock(side_effect=fake_get_user)

        msg = make_message(chat_id=chat.id, sender_id=sender)
        result = await svc.try_auto_respond(msg, chat.id, sender)

        assert result is None  # async mode -> None
        svc.agent_run_storage.create.assert_called_once()
        svc.kafka_producer.send.assert_called_once()
        call = svc.kafka_producer.send.call_args
        assert call.args[1]["responder_id"] == str(system_user_id)
        assert "invocation_kind_override" not in call.args[1]

    asyncio.run(go())


def test_try_auto_respond_async_enqueues_mention_override_for_out_of_chat_agent():
    async def go():
        svc = make_ai_service(async_mode=True)
        sender = uuid4()
        active_agent_id = uuid4()

        chat = Chat(
            org_id=uuid4(),
            type=ChatType.DIRECT,
            participants=[sender],
            active_agent=active_agent_id,
        )
        svc.chat_storage.get_by_id = AsyncMock(return_value=chat)

        async def fake_get_user(uid):
            user = MagicMock()
            user.id = uid
            user.is_system = uid == active_agent_id
            return user

        svc.user_storage.get_by_id = AsyncMock(side_effect=fake_get_user)

        msg = make_message(chat_id=chat.id, sender_id=sender)
        result = await svc.try_auto_respond(msg, chat.id, sender)

        assert result is None
        svc.kafka_producer.send.assert_called_once()
        call = svc.kafka_producer.send.call_args
        assert call.args[1]["responder_id"] == str(active_agent_id)
        assert call.args[1]["invocation_kind_override"] == "mention"

    asyncio.run(go())


def test_try_auto_respond_sync_passes_mention_override_for_out_of_chat_agent():
    async def go():
        svc = make_ai_service(async_mode=False)
        sender = uuid4()
        active_agent_id = uuid4()

        chat = Chat(
            org_id=uuid4(),
            type=ChatType.DIRECT,
            participants=[sender],
            active_agent=active_agent_id,
        )
        svc.chat_storage.get_by_id = AsyncMock(return_value=chat)

        async def fake_get_user(uid):
            user = MagicMock()
            user.id = uid
            user.is_system = uid == active_agent_id
            return user

        svc.user_storage.get_by_id = AsyncMock(side_effect=fake_get_user)
        svc.generate_response = AsyncMock(return_value=make_message(chat_id=chat.id))

        msg = make_message(chat_id=chat.id, sender_id=sender)
        await svc.try_auto_respond(msg, chat.id, sender)

        svc.generate_response.assert_called_once_with(
            message=msg,
            responder_id=active_agent_id,
            invocation_kind_override="mention",
        )

    asyncio.run(go())


def test_try_auto_respond_sync_keeps_participant_agent_direct():
    async def go():
        svc = make_ai_service(async_mode=False)
        sender = uuid4()
        system_user_id = uuid4()

        chat = Chat(
            org_id=uuid4(),
            type=ChatType.DIRECT,
            participants=[sender, system_user_id],
        )
        svc.chat_storage.get_by_id = AsyncMock(return_value=chat)

        async def fake_get_user(uid):
            user = MagicMock()
            user.id = uid
            user.is_system = uid == system_user_id
            return user

        svc.user_storage.get_by_id = AsyncMock(side_effect=fake_get_user)
        svc.generate_response = AsyncMock(return_value=make_message(chat_id=chat.id))

        msg = make_message(chat_id=chat.id, sender_id=sender)
        await svc.try_auto_respond(msg, chat.id, sender)

        svc.generate_response.assert_called_once_with(
            message=msg,
            responder_id=system_user_id,
            invocation_kind_override=None,
        )

    asyncio.run(go())


def test_generate_response_override_sets_mention_identity():
    async def go():
        role_id = uuid4()
        sender_id = uuid4()
        responder_id = uuid4()
        chat_id = uuid4()
        captured = {}

        svc = make_ai_service(async_mode=False)
        svc.user_storage.get_by_id = AsyncMock(
            return_value=User(
                id=responder_id,
                username="agent",
                role_id=role_id,
                is_system=True,
            )
        )
        svc.role_storage.get_by_id = AsyncMock(
            return_value=Role(id=role_id, code="agent", is_active=True)
        )
        svc.message_storage.list_by_chat = AsyncMock(return_value=[])

        async def fake_call_llm(*args, **kwargs):
            captured.update(kwargs)
            return AgentResult(content="ok", model="test", agent_type="simple"), {}

        svc._call_llm = fake_call_llm
        response = make_message(chat_id=chat_id, sender_id=responder_id)
        svc.persist_ai_message_with_modal = AsyncMock(return_value=response)

        msg = make_message(chat_id=chat_id, sender_id=sender_id)
        result = await svc.generate_response(
            msg,
            responder_id,
            invocation_kind_override="mention",
        )

        assert result == response
        assert captured["invocation_kind"] == "mention"
        assert captured["callee_user_id"] == responder_id

    asyncio.run(go())


def test_generate_response_explicit_mention_still_sets_mention_identity():
    async def go():
        role_id = uuid4()
        sender_id = uuid4()
        responder_id = uuid4()
        chat_id = uuid4()
        captured = {}

        svc = make_ai_service(async_mode=False)
        svc.user_storage.get_by_id = AsyncMock(
            return_value=User(
                id=responder_id,
                username="agent",
                role_id=role_id,
                is_system=True,
            )
        )
        svc.role_storage.get_by_id = AsyncMock(
            return_value=Role(id=role_id, code="agent", is_active=True)
        )
        svc.message_storage.list_by_chat = AsyncMock(return_value=[])

        async def fake_call_llm(*args, **kwargs):
            captured.update(kwargs)
            return AgentResult(content="ok", model="test", agent_type="simple"), {}

        svc._call_llm = fake_call_llm
        response = make_message(chat_id=chat_id, sender_id=responder_id)
        svc.persist_ai_message_with_modal = AsyncMock(return_value=response)

        mention = Mention(
            type=MentionType.AI_ROLE,
            user_id=responder_id,
            username="agent",
            position=0,
        )
        msg = make_message(chat_id=chat_id, sender_id=sender_id, mentions=[mention])
        result = await svc.generate_response(msg, responder_id)

        assert result == response
        assert captured["invocation_kind"] == "mention"
        assert captured["callee_user_id"] == responder_id

    asyncio.run(go())


def test_try_auto_respond_no_system_user_returns_none_and_no_enqueue():
    async def go():
        svc = make_ai_service(async_mode=True)
        sender = uuid4()
        other = uuid4()

        chat = Chat(
            org_id=uuid4(),
            type=ChatType.DIRECT,
            participants=[sender, other],
        )
        svc.chat_storage.get_by_id = AsyncMock(return_value=chat)

        async def fake_get_user(uid):
            user = MagicMock()
            user.id = uid
            user.is_system = False
            return user

        svc.user_storage.get_by_id = AsyncMock(side_effect=fake_get_user)

        msg = make_message(chat_id=chat.id, sender_id=sender)
        result = await svc.try_auto_respond(msg, chat.id, sender)

        assert result is None
        svc.agent_run_storage.create.assert_not_called()

    asyncio.run(go())


def test_has_pending_agent_runs_true_for_ai_mention():
    svc = make_ai_service(async_mode=True)
    msg = make_message(mentions=[
        Mention(type=MentionType.AI_ROLE, user_id=uuid4(), username="x", position=0),
    ])
    assert svc.has_pending_agent_runs(msg) is True


def test_has_pending_agent_runs_false_for_user_mention_only():
    svc = make_ai_service(async_mode=True)
    msg = make_message(mentions=[
        Mention(type=MentionType.USER, user_id=uuid4(), username="x", position=0),
    ])
    assert svc.has_pending_agent_runs(msg) is False
