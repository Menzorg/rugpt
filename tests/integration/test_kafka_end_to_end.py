"""
Integration test: Engine with real Kafka + Postgres.

Verifies:
1. AI mention @@role triggers a Kafka publish to agent.requests with correct payload
   (the consumer loop inside Engine handles the rest — not verified here because
   it requires a running Ollama and is covered by unit tests).
2. Fixtures cleanup everything they create.

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
