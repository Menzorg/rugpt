import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

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
    def __init__(self):
        self.created_llms = []

    def _create_llm(self, model_name, model_kwargs=None, extra_body=None):
        llm = SimpleNamespace(
            model=model_name,
            model_kwargs=model_kwargs,
            extra_body=extra_body,
        )
        self.created_llms.append(llm)
        return llm


class FakeCompiledAgent:
    def __init__(self, name):
        self.name = name
        self.bound_context = None

    def bind(self, **kwargs):
        self.bound_context = kwargs["context"]
        return self


class CapturingAgent:
    def __init__(self):
        self.name = "worker"
        self.input = None
        self.config = None

    def invoke(self, input_state, config=None):
        self.input = input_state
        self.config = config
        return {"messages": [HumanMessage(content="done")]}

    async def ainvoke(self, input_state, config=None):
        self.input = input_state
        self.config = config
        return {"messages": [HumanMessage(content="done")]}


def test_subagent_wrapper_invokes_agent_with_private_handoff_messages():
    agent = CapturingAgent()
    wrapper = supervisor._SubagentInputWrapper(agent, "worker")
    handoff_message = HumanMessage(content="private task")
    parent_message = HumanMessage(content="parent history")

    output = wrapper.invoke(
        {
            "messages": [parent_message],
            "subagent_messages": [handoff_message],
        },
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert output["messages"][0].content == "done"
    assert agent.input == {"messages": [handoff_message]}
    assert agent.config == {"configurable": {"thread_id": "thread-1"}}


def test_task_handoff_tool_preserves_parent_messages_and_sets_private_payload():
    handoff_tool = supervisor._create_task_handoff_tool(
        agent_name="doc_search",
        description=None,
        subagent_context="caller context",
    )

    command = handoff_tool.func(
        task="Find docs",
        details="Need recent invoices",
        state={"messages": [HumanMessage(content="parent history")]},
        tool_call_id="tool-call-1",
    )

    assert command.goto == "doc_search"
    assert isinstance(command.update["messages"][0], ToolMessage)
    assert command.update["messages"][0].tool_call_id == "tool-call-1"
    assert command.update["subagent_messages"][0].content == (
        "<context>\ncaller context\n</context>\n\n"
        "<task>\nFind docs\n</task>\n\n"
        "<details>\nNeed recent invoices\n</details>"
    )


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
        litellm_session_id="test-session",
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


@pytest.mark.asyncio
async def test_build_subagents_shares_litellm_session_and_sets_supervisor_metadata(monkeypatch):
    chat_id = uuid4()
    roles = [
        SimpleNamespace(
            code="doc-search",
            name="Document Search",
            description="Search documents",
            as_subagent_description=None,
            tools=[],
            model_name="test-model",
            agent_type="simple",
        ),
    ]
    fake_executor = FakeAgentExecutor()
    engine = SimpleNamespace(
        role_subagent_service=FakeRoleSubagentService(roles),
        tool_registry=FakeToolRegistry(),
        prompt_cache=FakePromptCache(),
        agent_executor=fake_executor,
    )
    engine_service = types.ModuleType("src.engine.services.engine_service")
    engine_service.get_engine_service = lambda: engine
    monkeypatch.setitem(sys.modules, "src.engine.services.engine_service", engine_service)

    def fake_create_agent(*, model, tools, system_prompt, context_schema, name):
        return FakeCompiledAgent(name)

    monkeypatch.setattr(supervisor, "create_agent", fake_create_agent)

    await supervisor._build_subagents(
        supervisor_role=SimpleNamespace(id=uuid4(), code="supervisor"),
        litellm_session_id="shared-session",
        chat_id=chat_id,
        supervisor_name="main_supervisor",
    )

    assert fake_executor.created_llms[0].extra_body == {
        "litellm_session_id": "shared-session",
        "metadata": {
            "agent_name": "doc_search",
            "chatid": str(chat_id),
            "supervisor_name": "main_supervisor",
        },
    }
