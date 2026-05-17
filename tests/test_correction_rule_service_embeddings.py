from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.logging_context import bind_correlation_id, correlation_id_var
from src.engine.models.message import Message, SenderType
from src.engine.services.correction_rule_service import CorrectionRuleService


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    async def aembed_query(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_search_corrections_passes_correlation_id_as_litellm_session_id():
    service = CorrectionRuleService.__new__(CorrectionRuleService)
    service._embeddings = FakeEmbeddings()
    service.correction_rule_storage = AsyncMock()
    service.correction_rule_storage.search_by_embeddings = AsyncMock(return_value=[])
    role_id = uuid4()

    token = bind_correlation_id("corr-emb-1")
    try:
        await service.search_corrections(
            "user prompt",
            "memory text",
            role_id=role_id,
        )
    finally:
        correlation_id_var.reset(token)

    assert len(service._embeddings.calls) == 2
    for _, kwargs in service._embeddings.calls:
        assert kwargs["extra_body"] == {
            "litellm_session_id": "corr-emb-1",
            "metadata": {
                "agent_name": "correction_rules_embedding",
                "chatid": "",
            },
        }

    service.correction_rule_storage.search_by_embeddings.assert_awaited_once_with(
        mem_embedding=[1.0, 2.0, 3.0],
        user_message_embedding=[1.0, 2.0, 3.0],
        top_k=3,
        role_id=role_id,
    )


@pytest.mark.asyncio
async def test_reject_and_create_rule_stores_embeddings_on_create():
    service = CorrectionRuleService.__new__(CorrectionRuleService)
    service._embeddings = FakeEmbeddings()
    service.kafka_producer = None
    service._extract_lesson = AsyncMock(return_value="lesson")

    role_id = uuid4()
    user_id = uuid4()
    ai_sender_id = uuid4()
    chat_id = uuid4()
    mem_id = uuid4()
    user_message_id = uuid4()
    ai_message_id = uuid4()

    original_message = Message(
        id=user_message_id,
        chat_id=chat_id,
        sender_id=user_id,
        sender_type=SenderType.USER,
        content="original user question",
        mem_id=mem_id,
    )
    ai_message = Message(
        id=ai_message_id,
        chat_id=chat_id,
        sender_id=ai_sender_id,
        sender_type=SenderType.AI_ROLE,
        content="bad answer",
        reply_to_id=user_message_id,
        mem_id=mem_id,
    )

    service.message_storage = AsyncMock()
    service.message_storage.get_by_id = AsyncMock(
        side_effect=[ai_message, original_message]
    )
    service.message_storage.reject = AsyncMock()

    service.user_storage = AsyncMock()
    service.user_storage.get_by_id = AsyncMock(
        side_effect=[
            SimpleNamespace(id=user_id, role_id=None),
            SimpleNamespace(id=ai_sender_id, role_id=role_id, is_system=False),
        ]
    )

    service.memory_snapshot_storage = AsyncMock()
    service.memory_snapshot_storage.get_by_id = AsyncMock(
        return_value=SimpleNamespace(id=mem_id, snapshot="memory snapshot text")
    )

    service.chat_service = AsyncMock()
    service.chat_service.send_message = AsyncMock(
        return_value=Message(chat_id=chat_id, sender_id=user_id, content="correction")
    )

    async def create_rule(rule):
        return rule

    service.correction_rule_storage = AsyncMock()
    service.correction_rule_storage.create = AsyncMock(side_effect=create_rule)

    created = await service.reject_and_create_rule(
        ai_message_id=ai_message_id,
        user_id=user_id,
        correction_text="correction",
    )

    assert created.mem_id == mem_id
    assert created.user_message_embedding == [1.0, 2.0, 3.0]
    assert created.mem_embedding == [1.0, 2.0, 3.0]
    assert service._embeddings.calls[0][0] == "original user question"
    assert service._embeddings.calls[1][0] == "memory snapshot text"
    service.correction_rule_storage.create.assert_awaited_once()
