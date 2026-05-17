import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.engine.agents.graphs import supervisor
from src.engine.agents.runtime import RuntimeContext


class FakeRoleSubagentService:
    def __init__(self, roles):
        self._roles = roles

    async def get_available_subagent_roles(self, supervisor_role_id):
        return self._roles


class FakeToolRegistry:
    def resolve(self, tool_names):
        return [object() for _ in tool_names], "tools doc"


class FakePromptCache:
    def get_prompt(self, role, is_subagent=False):
        return "Prompt with {tools}"


class FakeAgentExecutor:
    def _create_llm(self, model_name, model_kwargs=None):
        return SimpleNamespace(model=model_name, model_kwargs=model_kwargs)


class FakeCompiledAgent:
    def __init__(self, name):
        self.name = name
        self.bound_context = None

    def bind(self, **kwargs):
        self.bound_context = kwargs["context"]
        return self


@pytest.mark.asyncio
async def test_build_subagents_binds_separate_runtime_context_instances(monkeypatch):
    roles = [
        SimpleNamespace(
            code="doc_search",
            name="Document Search",
            description="Search documents",
            as_subagent_description=None,
            tools=["list_documents", "rag_search"],
            model_name="test-model",
            agent_type="simple",
        ),
        SimpleNamespace(
            code="task_helper",
            name="Task Helper",
            description="Work with tasks",
            as_subagent_description=None,
            tools=["task_query"],
            model_name="test-model",
            agent_type="simple",
        ),
    ]
    engine = SimpleNamespace(
        role_subagent_service=FakeRoleSubagentService(roles),
        tool_registry=FakeToolRegistry(),
        prompt_cache=FakePromptCache(),
        agent_executor=FakeAgentExecutor(),
    )
    engine_service = types.ModuleType("src.engine.services.engine_service")
    engine_service.get_engine_service = lambda: engine
    monkeypatch.setitem(sys.modules, "src.engine.services.engine_service", engine_service)

    created_agents = []

    def fake_create_agent(*, model, tools, system_prompt, context_schema, name):
        assert context_schema is RuntimeContext
        agent = FakeCompiledAgent(name)
        created_agents.append(agent)
        return agent

    monkeypatch.setattr(supervisor, "create_agent", fake_create_agent)

    subagents, descriptions = await supervisor._build_subagents(
        supervisor_role=SimpleNamespace(id=uuid4(), code="supervisor"),
        subagent_context="",
    )

    assert descriptions == {
        "doc_search": "Search documents",
        "task_helper": "Work with tasks",
    }
    assert len(subagents) == 2

    contexts = [agent.bound_context for agent in created_agents]
    assert contexts[0] is not contexts[1]
    assert all(isinstance(context, RuntimeContext) for context in contexts)
    assert [context.available_tools_count for context in contexts] == [2, 1]
