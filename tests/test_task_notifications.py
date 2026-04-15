"""
Tests for TaskNotificationService (item 10).

Mocks chat_service, message_storage, user_storage, kafka_producer.
Verifies:
- PM user lookup is cached after first call
- notify_take goes to creator, skips when creator missing or is actor
- notify_accept/reject/deadline/etc. address the opposite party
- overdue notifies both sides, deduplicated when creator == assignee
- every _post publishes to chat.events via kafka_producer
- chat.events publish failures don't break notification delivery
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.engine.models.chat import Chat, ChatType
from src.engine.models.task import Task
from src.engine.models.user import User
from src.engine.services.task_notification_service import TaskNotificationService


def make_user(uid=None, username="user", name="User"):
    return User(
        id=uid or uuid4(),
        org_id=uuid4(),
        name=name,
        username=username,
        email=f"{username}@test.com",
    )


def make_task(assignee_id, creator_id=None, org_id=None, title="T"):
    return Task(
        id=uuid4(),
        org_id=org_id or uuid4(),
        title=title,
        status="created",
        assignee_user_id=assignee_id,
        created_by_user_id=creator_id,
    )


def make_service(pm_user=None):
    """Build TaskNotificationService with all deps mocked."""
    chat_service = AsyncMock()
    chat_service.chat_storage = AsyncMock()
    chat_service.chat_storage.update_last_message = AsyncMock()
    chat_service.create_direct_chat = AsyncMock(
        return_value=Chat(org_id=uuid4(), type=ChatType.DIRECT, participants=[])
    )

    message_storage = AsyncMock()
    message_storage.create = AsyncMock(side_effect=lambda m: m)

    user_storage = AsyncMock()
    pm = pm_user or make_user(username="pm", name="PM-агент")
    user_storage.get_system_user_by_username = AsyncMock(return_value=pm)
    user_storage.get_by_id = AsyncMock()  # configured per-test

    kafka_producer = AsyncMock()
    kafka_producer.send = AsyncMock()

    svc = TaskNotificationService(
        chat_service=chat_service,
        message_storage=message_storage,
        user_storage=user_storage,
        kafka_producer=kafka_producer,
    )
    return svc, chat_service, message_storage, user_storage, kafka_producer


def test_pm_user_lookup_cached():
    async def go():
        svc, _, _, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=creator)

        await svc.notify_take(task, assignee)
        await svc.notify_take(task, assignee)

        # PM user resolved once, cached for second call
        assert user_storage.get_system_user_by_username.call_count == 1

    asyncio.run(go())


def test_notify_take_sends_to_creator_and_publishes_kafka():
    async def go():
        svc, chat_service, message_storage, user_storage, kafka_producer = make_service()
        assignee = make_user(username="bob", name="Bob")
        creator = make_user(username="alice", name="Alice")
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=creator)

        await svc.notify_take(task, assignee)

        # Direct chat created for recipient=creator
        chat_service.create_direct_chat.assert_called_once()
        call = chat_service.create_direct_chat.call_args
        # positional args: (pm_id, recipient_user_id, recipient_org_id)
        assert call.args[1] == creator.id
        assert call.args[2] == task.org_id

        # Message persisted
        message_storage.create.assert_called_once()
        posted_msg = message_storage.create.call_args.args[0]
        assert "Bob" in posted_msg.content or "@bob" in posted_msg.content
        assert task.title in posted_msg.content

        # kafka chat.events publish
        kafka_producer.send.assert_called_once()
        send_call = kafka_producer.send.call_args
        assert send_call.args[0] == "chat.events"
        assert "chat_id" in send_call.args[1]
        assert "message" in send_call.args[1]

    asyncio.run(go())


def test_notify_take_skips_legacy_task_no_creator():
    async def go():
        svc, chat_service, _, user_storage, _ = make_service()
        assignee = make_user()
        task = make_task(assignee.id, creator_id=None)  # legacy, no creator
        user_storage.get_by_id = AsyncMock(return_value=None)

        await svc.notify_take(task, assignee)

        chat_service.create_direct_chat.assert_not_called()

    asyncio.run(go())


def test_notify_take_skips_when_actor_is_creator():
    async def go():
        svc, chat_service, _, user_storage, _ = make_service()
        creator = make_user()
        task = make_task(creator.id, creator.id)  # self-assigned
        user_storage.get_by_id = AsyncMock(return_value=creator)

        await svc.notify_take(task, creator)

        chat_service.create_direct_chat.assert_not_called()

    asyncio.run(go())


def test_notify_accept_addresses_assignee():
    async def go():
        svc, chat_service, message_storage, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=assignee)

        await svc.notify_accept(task, creator)

        chat_service.create_direct_chat.assert_called_once()
        assert chat_service.create_direct_chat.call_args.args[1] == assignee.id
        msg = message_storage.create.call_args.args[0]
        assert "принят" in msg.content.lower()

    asyncio.run(go())


def test_notify_reject_includes_comment():
    async def go():
        svc, _, message_storage, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=assignee)

        await svc.notify_reject(task, creator, comment="нужно доделать раздел X")

        msg = message_storage.create.call_args.args[0]
        assert "нужно доделать раздел X" in msg.content

    asyncio.run(go())


def test_notify_reject_without_comment_still_sends():
    async def go():
        svc, _, message_storage, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=assignee)

        await svc.notify_reject(task, creator, comment=None)

        msg = message_storage.create.call_args.args[0]
        assert task.title in msg.content

    asyncio.run(go())


def test_notify_propose_deadline_addresses_creator():
    async def go():
        svc, chat_service, message_storage, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        task.proposed_deadline = datetime(2026, 5, 1, 15, 0)
        user_storage.get_by_id = AsyncMock(return_value=creator)

        await svc.notify_propose_deadline(task, assignee)

        assert chat_service.create_direct_chat.call_args.args[1] == creator.id
        msg = message_storage.create.call_args.args[0]
        assert "01.05.2026" in msg.content

    asyncio.run(go())


def test_notify_overdue_notifies_both():
    async def go():
        svc, chat_service, _, user_storage, _ = make_service()
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)

        async def fake_get_by_id(uid):
            return assignee if uid == assignee.id else creator

        user_storage.get_by_id = AsyncMock(side_effect=fake_get_by_id)

        await svc.notify_overdue(task)

        assert chat_service.create_direct_chat.call_count == 2
        recipients = {
            call.args[1] for call in chat_service.create_direct_chat.call_args_list
        }
        assert recipients == {assignee.id, creator.id}

    asyncio.run(go())


def test_notify_overdue_dedupes_when_creator_is_assignee():
    async def go():
        svc, chat_service, _, user_storage, _ = make_service()
        person = make_user()
        task = make_task(person.id, person.id)
        user_storage.get_by_id = AsyncMock(return_value=person)

        await svc.notify_overdue(task)

        # Only one notification even though creator == assignee
        assert chat_service.create_direct_chat.call_count == 1

    asyncio.run(go())


def test_kafka_publish_failure_does_not_break_notification():
    async def go():
        svc, _, message_storage, user_storage, kafka_producer = make_service()
        kafka_producer.send = AsyncMock(side_effect=RuntimeError("broker down"))
        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        user_storage.get_by_id = AsyncMock(return_value=creator)

        # Must not raise — DB persist already happened, Kafka error is best-effort
        await svc.notify_take(task, assignee)

        message_storage.create.assert_called_once()

    asyncio.run(go())


def test_missing_pm_user_is_graceful():
    async def go():
        pm_user_storage = AsyncMock()
        pm_user_storage.get_system_user_by_username = AsyncMock(return_value=None)

        svc = TaskNotificationService(
            chat_service=AsyncMock(),
            message_storage=AsyncMock(),
            user_storage=pm_user_storage,
            kafka_producer=AsyncMock(),
        )

        assignee = make_user()
        creator = make_user()
        task = make_task(assignee.id, creator.id)
        pm_user_storage.get_by_id = AsyncMock(return_value=creator)

        # Should not raise; just logs and no-ops
        await svc.notify_take(task, assignee)

    asyncio.run(go())
