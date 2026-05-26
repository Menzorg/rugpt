from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.agents.executor import AgentExecutor
from src.engine.agents.result import AgentResult


class FakePromptCache:
    def get_prompt(self, role, org_context="", is_subagent=False):
        return "System prompt with {tools}"


class FakeToolRegistry:
    def resolve(self, tools):
        return [], ""


class FakeLLM:
    model = "test-model"

    def bind(self, **kwargs):
        return self


@pytest.mark.asyncio
async def test_execute_without_chat_id_is_system_and_skips_memory_and_corrections(monkeypatch):
    caller_user_id = uuid4()
    callee_user_id = uuid4()
    org_id = uuid4()
    captured = {}

    executor = AgentExecutor(
        base_url="http://localhost:4000",
        default_model="test-model",
        prompt_cache=FakePromptCache(),
        tool_registry=FakeToolRegistry(),
    )
    executor.memory_service = AsyncMock()
    executor.correction_rule_service = AsyncMock()
    monkeypatch.setattr(
        executor,
        "_create_llm",
        lambda *args, **kwargs: FakeLLM(),
    )

    async def fake_run_simple_agent(**kwargs):
        captured["config"] = kwargs["config"]
        captured["messages"] = kwargs["messages"]
        kwargs["context_schema"].called_modals.append({"title": "T"})
        return AgentResult(content="ok", model="test-model", agent_type="simple")

    monkeypatch.setattr(
        "src.engine.agents.executor.run_simple_agent",
        fake_run_simple_agent,
    )
    monkeypatch.setattr("src.engine.agents.executor.count_tokens", lambda *args, **kwargs: 1)

    engine = SimpleNamespace(
        user_storage=SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(
                    id=caller_user_id,
                    org_id=org_id,
                    name="Caller",
                    username="caller",
                    email=None,
                    is_admin=False,
                    department_id=None,
                    is_head=False,
                )
            )
        ),
        org_storage=SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(org_context="", timezone="Europe/Moscow")),
        ),
    )
    monkeypatch.setattr(
        "src.engine.services.engine_service.get_engine_service",
        lambda: engine,
    )

    role = SimpleNamespace(
        id=uuid4(),
        org_id=org_id,
        code="reasoner",
        agent_type="simple",
        agent_config={},
        tools=[],
        model_name="test-model",
    )

    result, metadata = await executor.execute(
        role=role,
        messages=[{"role": "user", "content": "Build a report"}],
        caller_user_id=caller_user_id,
        callee_user_id=callee_user_id,
        invocation_kind="mention",
        chat_id=None,
    )

    assert result.content == "ok"
    assert metadata == {"modal": {"title": "T"}}
    assert captured["config"]["configurable"]["invocation_kind"] == "system"
    assert captured["config"]["configurable"]["callee_user_id"] == str(caller_user_id)
    executor.memory_service.get_summary_for_chat.assert_not_called()
    executor.memory_service.check_resummary_needed.assert_not_called()
    executor.correction_rule_service.search_corrections.assert_not_called()


@pytest.mark.asyncio
async def test_execute_uses_explicit_agent_name_in_prompt(monkeypatch):
    caller_user_id = uuid4()
    role_id = uuid4()
    org_id = uuid4()
    chat_id = uuid4()
    captured = {}

    executor = AgentExecutor(
        base_url="http://localhost:4000",
        default_model="test-model",
        prompt_cache=FakePromptCache(),
        tool_registry=FakeToolRegistry(),
    )
    monkeypatch.setattr(executor, "_create_llm", lambda *args, **kwargs: FakeLLM())

    async def fake_run_simple_agent(**kwargs):
        captured["system_prompt"] = kwargs["system_prompt"]
        return AgentResult(content="ok", model="test-model", agent_type="simple")

    monkeypatch.setattr("src.engine.agents.executor.run_simple_agent", fake_run_simple_agent)
    monkeypatch.setattr("src.engine.agents.executor.count_tokens", lambda *args, **kwargs: 1)

    caller = SimpleNamespace(
        id=caller_user_id,
        org_id=org_id,
        role_id=role_id,
        name="Caller",
        username="caller",
        email=None,
        is_admin=False,
        department_id=None,
        is_head=False,
    )
    engine = SimpleNamespace(
        user_storage=SimpleNamespace(get_by_id=AsyncMock(return_value=caller)),
        org_storage=SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(org_context="", timezone="Europe/Moscow"))
        ),
        chat_storage=SimpleNamespace(
            get_attachments=AsyncMock(return_value=[]),
        ),
        user_file_storage=SimpleNamespace(get_many_by_ids=AsyncMock(return_value={})),
    )
    monkeypatch.setattr("src.engine.services.engine_service.get_engine_service", lambda: engine)

    role = SimpleNamespace(
        id=role_id,
        org_id=org_id,
        code="reasoner",
        agent_type="simple",
        agent_config={},
        tools=[],
        model_name="test-model",
    )

    await executor.execute(
        role=role,
        messages=[{"role": "user", "content": "Help"}],
        caller_user_id=caller_user_id,
        invocation_kind="direct",
        chat_id=chat_id,
        agent_name="mirror",
    )

    assert "Твоё имя: mirror." in captured["system_prompt"]
    assert "Твоё имя: reasoner." not in captured["system_prompt"]
    assert "##ВАЖНЫЕ ОГРАНИЧЕНИЯ НА УРОВНЕ СИСТЕМЫ" in captured["system_prompt"]
    assert "ты являешься основным агентом в этом чате" in captured["system_prompt"]
