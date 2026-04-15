"""
Integration test: Engine with real Kafka + Postgres.

Verifies:
1. PM notifications travel from TaskService.take_task through TaskNotificationService,
   get persisted in messages table, and are published to chat.events in a form that
   a fresh aiokafka consumer can read.
2. AI mention @@role triggers a Kafka publish to agent.requests with correct payload
   (the consumer loop inside Engine handles the rest — not verified here because
   it requires a running Ollama and is covered by unit tests).
3. Fixtures cleanup everything they create.

Skipped if Kafka is not reachable at localhost:9092.
"""
import asyncio
import json
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import asyncpg

from src.engine.config import Config


KAFKA_BROKERS = Config.KAFKA_BOOTSTRAP_SERVERS


def _kafka_reachable() -> bool:
    try:
        import socket
        host, port = KAFKA_BROKERS.split(",")[0].split(":")
        s = socket.create_connection((host, int(port)), timeout=1.0)
        s.close()
        return True
    except Exception:
        return False


if not _kafka_reachable():
    pytest.skip("Kafka broker not reachable", allow_module_level=True)


async def _fetch_ids_from_dev_db():
    """Grab real org+user IDs from dev DB for FK satisfaction.

    Finds an org (non-system) with at least two active non-system users.
    """
    conn = await asyncpg.connect(Config.get_postgres_dsn())
    try:
        row = await conn.fetchrow(
            """
            SELECT u.org_id AS org_id
              FROM users u
             WHERE u.is_active = true AND u.is_system = false
               AND u.org_id != '00000000-0000-0000-0000-000000000000'::uuid
             GROUP BY u.org_id
            HAVING COUNT(*) >= 2
             LIMIT 1
            """
        )
        if not row:
            return None
        org_id = row["org_id"]
        users = await conn.fetch(
            "SELECT * FROM users WHERE org_id=$1 AND is_active=true AND is_system=false LIMIT 2",
            org_id,
        )
        if len(users) < 2:
            return None
        return org_id, users[0], users[1]
    finally:
        await conn.close()


def test_pm_notification_published_to_chat_events():
    """take_task -> PM notification -> chat.events (verified by fresh consumer)."""
    async def go():
        from aiokafka import AIOKafkaConsumer
        from src.engine.services.engine_service import init_engine_service
        from src.engine.models.user import User

        fixtures = await _fetch_ids_from_dev_db()
        if fixtures is None:
            pytest.skip("dev DB missing required fixtures (org + 2 users)")
        org_id, admin_row, chu_row = fixtures

        def _u(r):
            return User(
                id=r["id"], org_id=r["org_id"], name=r["name"],
                username=r["username"], email=r["email"],
                is_admin=r["is_admin"], is_active=r["is_active"],
                is_head=r.get("is_head", False),
            )

        admin = _u(admin_row)
        assignee = _u(chu_row)

        # Subscribe BEFORE triggering the task transition
        consumer = AIOKafkaConsumer(
            Config.KAFKA_TOPIC_CHAT_EVENTS,
            bootstrap_servers=KAFKA_BROKERS,
            group_id=f"integ-pm-{uuid4()}",
            auto_offset_reset="latest",
            value_deserializer=lambda v: json.loads(v.decode()),
        )
        await consumer.start()

        # Reset singleton so each test gets a fresh EngineService bound to this
        # asyncio event loop (asyncio.run creates a new loop per test).
        import src.engine.services.engine_service as _es
        _es._engine_service = None
        engine = await init_engine_service()

        task = None
        try:
            task = await engine.task_service.create(
                org_id=org_id,
                title="integ-pm-task",
                assignee_user_id=assignee.id,
                created_by_user_id=admin.id,
            )
            # Assignee takes -> creator should get PM notification
            await engine.task_service.take_task(task.id, assignee)

            # Wait up to 5s for chat.events message
            msg = await asyncio.wait_for(consumer.__anext__(), timeout=5.0)
            payload = msg.value
            assert "chat_id" in payload
            assert "message" in payload
            content = payload["message"]["content"]
            assert task.title in content
            assert "в работу" in content

        finally:
            # Cleanup
            if task is not None:
                conn = await asyncpg.connect(Config.get_postgres_dsn())
                try:
                    await conn.execute(
                        "DELETE FROM task_events WHERE task_id=$1", task.id
                    )
                    await conn.execute(
                        "DELETE FROM chats WHERE task_id=$1", task.id
                    )
                    # delete any direct chats with pm that were created for this test
                    pm_row = await conn.fetchrow(
                        "SELECT id FROM users WHERE username='pm' AND is_system=true"
                    )
                    if pm_row:
                        await conn.execute(
                            "DELETE FROM messages WHERE chat_id IN "
                            "(SELECT id FROM chats WHERE $1 = ANY(participants::uuid[]) AND type='direct')",
                            str(pm_row["id"]),
                        )
                        await conn.execute(
                            "DELETE FROM chats WHERE type='direct' AND $1 = ANY(participants::uuid[]) "
                            "AND updated_at > NOW() - INTERVAL '1 minute'",
                            str(pm_row["id"]),
                        )
                    await conn.execute(
                        "DELETE FROM in_app_notifications WHERE reference_id=$1", task.id
                    )
                    await conn.execute("DELETE FROM tasks WHERE id=$1", task.id)
                finally:
                    await conn.close()

            await consumer.stop()

    asyncio.run(go())


def test_ai_mention_publishes_agent_request():
    """Sending a message with @@role mention publishes to agent.requests topic."""
    async def go():
        from aiokafka import AIOKafkaConsumer
        from src.engine.services.engine_service import init_engine_service
        from src.engine.models.message import Message, Mention, MentionType, SenderType

        # Reset singleton so each test gets a fresh EngineService bound to this
        # asyncio event loop (asyncio.run creates a new loop per test).
        import src.engine.services.engine_service as _es
        _es._engine_service = None
        engine = await init_engine_service()

        # Subscribe to agent.requests BEFORE sending
        consumer = AIOKafkaConsumer(
            Config.KAFKA_TOPIC_AGENT_REQUESTS,
            bootstrap_servers=KAFKA_BROKERS,
            group_id=f"integ-agent-{uuid4()}",
            auto_offset_reset="latest",
            value_deserializer=lambda v: json.loads(v.decode()),
        )
        await consumer.start()

        try:
            # Find any system AI user to mention
            conn = await asyncpg.connect(Config.get_postgres_dsn())
            ai_row = await conn.fetchrow(
                "SELECT id, username FROM users WHERE is_system=true AND username != 'pm' LIMIT 1"
            )
            chat_row = await conn.fetchrow("SELECT id FROM chats LIMIT 1")
            user_row = await conn.fetchrow(
                "SELECT id FROM users WHERE is_system=false LIMIT 1"
            )
            await conn.close()
            if not (ai_row and chat_row and user_row):
                pytest.skip("dev DB missing required fixtures")

            # Build a fake Message with an AI mention and call process_ai_mentions
            msg = Message(
                id=uuid4(),
                chat_id=chat_row["id"],
                sender_id=user_row["id"],
                sender_type=SenderType.USER,
                content=f"@@{ai_row['username']} привет",
                mentions=[
                    Mention(
                        type=MentionType.AI_ROLE,
                        user_id=ai_row["id"],
                        username=ai_row["username"],
                        position=0,
                    )
                ],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            # Persist minimal message for FK (agent_run references user_message_id)
            msg = await engine.message_storage.create(msg)

            result = await engine.ai_service.process_ai_mentions(msg, org_id=uuid4())
            assert result == [], "async mode should return empty ai_responses list"

            # Wait for agent.requests message
            kafka_msg = await asyncio.wait_for(consumer.__anext__(), timeout=5.0)
            payload = kafka_msg.value
            assert payload["user_message_id"] == str(msg.id)
            assert payload["responder_id"] == str(ai_row["id"])

            # Verify agent_run row was created
            run = None
            conn = await asyncpg.connect(Config.get_postgres_dsn())
            try:
                row = await conn.fetchrow(
                    "SELECT * FROM agent_runs WHERE request_id = $1",
                    UUID(payload["request_id"]),
                )
                assert row is not None
                assert row["chat_id"] == msg.chat_id
            finally:
                # Cleanup
                await conn.execute("DELETE FROM agent_runs WHERE request_id = $1",
                                   UUID(payload["request_id"]))
                await conn.execute("DELETE FROM messages WHERE id = $1", msg.id)
                await conn.close()

        finally:
            await consumer.stop()

    asyncio.run(go())
