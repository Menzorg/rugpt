from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.logging_context import bind_correlation_id, correlation_id_var
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
