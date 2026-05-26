import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.engine.services.in_app_notification_service import InAppNotificationService


def _run(coro):
    return asyncio.run(coro)


def test_create_publishes_notification_to_chat_events():
    storage = MagicMock()
    storage.create = AsyncMock(side_effect=lambda n: n)
    kafka = MagicMock()
    kafka.send = AsyncMock()
    svc = InAppNotificationService(storage, kafka_producer=kafka)

    uid = uuid4()
    _run(svc.create(
        user_id=uid, org_id=uuid4(), type="system",
        title="t", content="c",
        reference_type="support_ticket", reference_id=uuid4(),
    ))

    kafka.send.assert_awaited_once()
    args = kafka.send.await_args
    assert args.args[0] == "chat.events"
    payload = args.args[1]
    assert payload["kind"] == "notification"
    assert payload["user_id"] == str(uid)
    assert payload["notification"]["reference_type"] == "support_ticket"


def test_create_no_kafka_is_noop():
    storage = MagicMock()
    storage.create = AsyncMock(side_effect=lambda n: n)
    svc = InAppNotificationService(storage)  # no kafka_producer
    _run(svc.create(user_id=uuid4(), org_id=uuid4(), type="system", title="t"))
    # No exception → persist path unaffected.
