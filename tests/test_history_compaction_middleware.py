import importlib.util
import sys
import types
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import pytest


@pytest.fixture()
def history_compaction_middleware_cls(monkeypatch):
    module_name = "src.engine.agents.middleware"
    sys.modules.pop(module_name, None)
    sys.modules.pop("src.engine.agents", None)

    agents_package = types.ModuleType("src.engine.agents")
    agents_package.__path__ = ["src/engine/agents"]
    monkeypatch.setitem(sys.modules, "src.engine.agents", agents_package)

    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = object
    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)

    runtime = types.ModuleType("src.engine.agents.runtime")
    runtime.RuntimeContext = object
    monkeypatch.setitem(sys.modules, "src.engine.agents.runtime", runtime)

    token_counter = types.ModuleType("src.engine.utils.token_counter")
    token_counter.count_tokens = lambda text: len(str(text).split())
    token_counter.count_tokens_messages = lambda messages: len(messages)
    monkeypatch.setitem(sys.modules, "src.engine.utils.token_counter", token_counter)

    token_logger = types.ModuleType("src.engine.utils.token_logger")
    token_logger.log_llm_tokens = lambda *args, **kwargs: 0
    token_logger.log_token_summary = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "src.engine.utils.token_logger", token_logger)

    spec = importlib.util.spec_from_file_location(
        module_name,
        "src/engine/agents/middleware.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)

    yield module.HistoryCompactionMiddleware

    sys.modules.pop(module_name, None)


def test_compaction_keep_window_prepends_latest_human_when_missing(history_compaction_middleware_cls):
    middleware = history_compaction_middleware_cls(
        llm=SimpleNamespace(model="test"),
        trigger_tokens=0,
        keep_last=3,
    )
    messages = [
        HumanMessage(content="Older prompt"),
        AIMessage(content="Older answer"),
        HumanMessage(content="Latest prompt"),
        AIMessage(content="Working"),
        ToolMessage(content="Tool result", tool_call_id="call-1"),
        AIMessage(content="Final draft"),
    ]
    tail = messages[-3:]

    to_keep = middleware._prepend_latest_human_if_missing(messages, tail)

    assert to_keep == [messages[2], *tail]


def test_compaction_keep_window_is_unchanged_when_human_is_present(history_compaction_middleware_cls):
    middleware = history_compaction_middleware_cls(
        llm=SimpleNamespace(model="test"),
        trigger_tokens=0,
        keep_last=3,
    )
    messages = [
        HumanMessage(content="Older prompt"),
        AIMessage(content="Older answer"),
        HumanMessage(content="Latest prompt"),
        AIMessage(content="Latest answer"),
    ]
    tail = messages[-3:]

    to_keep = middleware._prepend_latest_human_if_missing(messages, tail)

    assert to_keep == tail
