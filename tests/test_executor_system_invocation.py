from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.engine.agents.executor import AgentExecutor
from src.engine.agents.result import AgentResult
from src.engine.models.chat import Chat, ChatType
from src.engine.models.support_ticket import (
    ClosedByRole,
    SupportTicket,
    SupportTicketCategory,
    SupportTicketStatus,
)


class FakePromptCache:
    def get_prompt(self, role, org_context="", is_subagent=False, timezone="Europe/Moscow"):
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
            get_by_id=AsyncMock(return_value=None),
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


@pytest.mark.asyncio
async def test_build_chat_context_support_chat_includes_ticket_details():
    requester_id = uuid4()
    assignee_id = uuid4()
    org_id = uuid4()
    ticket_id = uuid4()
    chat_id = uuid4()

    executor = AgentExecutor(
        base_url="http://localhost:4000",
        default_model="test-model",
        prompt_cache=FakePromptCache(),
        tool_registry=FakeToolRegistry(),
    )

    chat = Chat(
        id=chat_id,
        org_id=org_id,
        type=ChatType.SUPPORT,
        participants=[requester_id, assignee_id],
        support_ticket_id=ticket_id,
    )
    ticket = SupportTicket(
        id=ticket_id,
        requester_user_id=requester_id,
        requester_org_id=org_id,
        category=SupportTicketCategory.BUG,
        status=SupportTicketStatus.CLOSED,
        assignee_user_id=assignee_id,
        ai_first_response_at=datetime(2026, 1, 2, 3, 4, 5),
        ai_handoff_at=datetime(2026, 1, 2, 4, 5, 6),
        closed_at=datetime(2026, 1, 3, 5, 6, 7),
        closed_by_user_id=assignee_id,
        closed_by_role=ClosedByRole.OPERATOR,
        title="Broken integration",
        created_at=datetime(2026, 1, 1, 1, 2, 3),
        updated_at=datetime(2026, 1, 3, 6, 7, 8),
    )
    requester = SimpleNamespace(
        id=requester_id,
        name="Requester",
        username="requester",
        email="requester@example.com",
        department_name=None,
    )
    assignee = SimpleNamespace(
        id=assignee_id,
        name="Operator",
        username="operator",
        email="operator@example.com",
        department_name="Support",
    )

    engine = SimpleNamespace(
        chat_storage=SimpleNamespace(
            get_by_id=AsyncMock(return_value=chat),
            get_attachments=AsyncMock(return_value=[]),
        ),
        support_ticket_storage=SimpleNamespace(
            get_by_id=AsyncMock(return_value=ticket),
        ),
        user_storage=SimpleNamespace(
            get_by_id=AsyncMock(side_effect=lambda user_id: {
                requester_id: requester,
                assignee_id: assignee,
            }.get(user_id)),
        ),
        org_storage=SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(id=org_id, name="Acme")),
        ),
        user_file_storage=SimpleNamespace(get_many_by_ids=AsyncMock(return_value={})),
    )

    chat_type_block, participants_block, attachments_block = await executor._build_chat_context(
        engine,
        chat_id,
    )

    assert attachments_block is None
    assert f"ID тикета: {ticket_id}" in chat_type_block
    assert "Категория: bug" in chat_type_block
    assert "Статус: closed" in chat_type_block
    assert "Заголовок: Broken integration" in chat_type_block
    assert "Заявитель: Requester (@requester" in chat_type_block
    assert "Организация заявителя: Acme" in chat_type_block
    assert "Оператор: Operator (@operator" in chat_type_block
    assert "Первый ответ AI: 2026-01-02T03:04:05" in chat_type_block
    assert "Передано оператору: 2026-01-02T04:05:06" in chat_type_block
    assert "Закрыто ролью: operator" in chat_type_block
    assert "Requester" in participants_block
    assert "Operator" in participants_block
