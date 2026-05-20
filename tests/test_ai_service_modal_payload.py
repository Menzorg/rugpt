"""Verify that when an agent run produces a show_modal tool call, the resulting
AI message has its modal payload attached to messages.metadata.modal.

Plan-1 Task 8 (modal-protocol-infra).
"""
import os
import json
import pytest
import pytest_asyncio
from uuid import uuid4
import asyncpg

from src.engine.services.engine_service import get_engine_service
from src.engine.agents.result import AgentResult, ToolCall


DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    engine = get_engine_service()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(), 'm', $1) RETURNING id",
            f"modal_{uuid4().hex[:8]}",
        )
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true) RETURNING id",
            org_id, f"u_{uuid4().hex[:6]}", f"u_{uuid4()}@t.local"
        )
        chat_id = await conn.fetchval(
            "INSERT INTO chats (id, org_id, type, participants) "
            "VALUES (gen_random_uuid(), $1, 'direct', ARRAY[$2::text]) RETURNING id",
            org_id, str(user_id)
        )
    yield {
        "engine": engine,
        "org_id": org_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "pool": pool,
    }
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    await pool.close()


@pytest.mark.asyncio(loop_scope="module")
async def test_modal_payload_attached_to_message(env):
    """ai_service.persist_ai_message_with_modal extracts <<MODAL_EMITTED>> tag
    from a tool call result and writes payload to messages.metadata.modal."""
    engine = env["engine"]

    # Simulate an AgentResult that called show_modal once.
    modal_payload = {
        "title": "Утвердить?",
        "body": "test body",
        "actions": [
            {"label": "Да", "action_type": "dummy_acknowledge", "params": {"target_id": "x"}}
        ],
        "target": {"type": "test", "id": "x"},
    }
    tool_result_str = (
        f"<<MODAL_EMITTED>>{json.dumps(modal_payload, ensure_ascii=False)}<</MODAL_EMITTED>>"
    )
    result = AgentResult(
        content="Готово.",
        model="test",
        agent_type="simple",
        tool_calls=[
            ToolCall(tool_name="show_modal", tool_input={}, tool_output=tool_result_str)
        ],
        tokens_used=0,
        finish_reason="stop",
        error=None,
    )

    msg = await engine.ai_service.persist_ai_message_with_modal(
        chat_id=env["chat_id"],
        sender_id=env["user_id"],
        agent_result=result,
        role_id=None,
    )
    assert msg.metadata.get("modal") == modal_payload
