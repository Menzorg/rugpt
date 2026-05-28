"""
Tests for the ChatOpenAI None-content workaround in executor.py.

Root cause: vLLM returns 400 "can only concatenate str (not 'NoneType') to str"
when an assistant message has content=null together with tool_calls. This happens
when a model (e.g. Gemma 4) decides to call a tool without emitting any text —
langchain_openai upstream does NOT normalize this case, so content=None reaches
the wire on the next LangGraph turn.

Our ChatOpenAI subclass patches _get_request_payload to replace any
content=None with "" before the payload reaches the wire.

Section A — payload-level unit tests (no network).
Section B — integration tests against the LiteLLM proxy configured in .env
             (LLM_BASE_URL / LLM_API_KEY / DEFAULT_MODEL).
             Marked @pytest.mark.integration; skipped when proxy is unreachable.
"""

import json
import logging

import httpx
import pytest
from langchain_openai import ChatOpenAI as _UpstreamChatOpenAI

from src.engine.agents.executor import ChatOpenAI
from src.engine.config import Config

logger = logging.getLogger(__name__)

# The exact payload that causes the 400:
# assistant message has content=None with tool_calls — upstream passes it as-is,
# vLLM crashes with "can only concatenate str (not 'NoneType') to str".
# Note: tool_call_id mismatch (call_ABC vs call_XYZ) is intentional —
# it matches the real production scenario where the tool response arrives
# with a different id, and the template can't even get that far due to the null.
MESSAGES_WITH_NULL_CONTENT = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "What time is it?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_ABC",
                "type": "function",
                "function": {"name": "get_time", "arguments": "{}"},
            }
        ],
    },
    {"role": "tool", "content": "15:00 UTC", "tool_call_id": "call_XYZ"},
    {"role": "user", "content": "thanks"},
]


def _make_llm(cls=ChatOpenAI):
    return cls(
        base_url=Config.LLM_BASE_URL,
        api_key=Config.LLM_API_KEY,
        model=Config.DEFAULT_MODEL,
    )


def _proxy_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{Config.LLM_BASE_URL}/models",
            headers={"Authorization": f"Bearer {Config.LLM_API_KEY}"},
            timeout=3,
        )
        models = [m["id"] for m in resp.json().get("data", [])]
        return Config.DEFAULT_MODEL in models
    except Exception:
        return False


integration = pytest.mark.skipif(
    not _proxy_reachable(),
    reason=f"LiteLLM proxy not reachable at {Config.LLM_BASE_URL}",
)


# ---------------------------------------------------------------------------
# Section A — payload-level unit tests (no network)
# ---------------------------------------------------------------------------


def test_upstream_chatopenai_passes_null_content_through():
    """Vanilla ChatOpenAI leaves content=None on assistant+tool_calls — the bug."""
    llm = _make_llm(_UpstreamChatOpenAI)
    payload = llm._get_request_payload(MESSAGES_WITH_NULL_CONTENT)
    assistant_msg = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert assistant_msg["content"] is None


def test_executor_chatopenai_replaces_null_content_with_empty_string():
    """Our override replaces content=None with "" — prevents vLLM 400."""
    llm = _make_llm()
    payload = llm._get_request_payload(MESSAGES_WITH_NULL_CONTENT)
    for msg in payload["messages"]:
        assert msg.get("content") is not None, (
            f"content=None leaked through for role={msg.get('role')}. "
            "If you are running Gemma 4, this can be fixed by using the fixed chat template "
            "that normalizes null content in assistant+tool_calls messages."
        )
    assistant_msg = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert assistant_msg["content"] == "", (
        f"Expected content='' for assistant message, got {assistant_msg['content']!r}. "
        "If you are running Gemma 4, this can be fixed by using the fixed chat template "
        "that normalizes null content in assistant+tool_calls messages."
    )


def test_executor_chatopenai_does_not_alter_non_null_content():
    """The patch must not touch messages that already have string content."""
    llm = _make_llm()
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    payload = llm._get_request_payload(messages)
    contents = {m["role"]: m["content"] for m in payload["messages"]}
    assert contents["user"] == "hello"
    assert contents["assistant"] == "hi there"


# ---------------------------------------------------------------------------
# Section B — integration tests (real LiteLLM proxy from .env)
# ---------------------------------------------------------------------------


@integration
def test_integration_fixed_null_content_returns_200():
    """Fixed payload with content='' must succeed — confirms our patch works."""
    llm = _make_llm()
    payload = llm._get_request_payload(MESSAGES_WITH_NULL_CONTENT)
    logger.info(
        "sending fixed payload (content='') to %s model=%s:\n%s",
        Config.LLM_BASE_URL,
        Config.DEFAULT_MODEL,
        json.dumps(payload["messages"], indent=2, default=str),
    )
    resp = httpx.post(
        f"{Config.LLM_BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {Config.LLM_API_KEY}"},
        timeout=60,
    )
    logger.info("response status: %d", resp.status_code)
    if resp.status_code == 200:
        logger.info("model reply: %s", resp.json()["choices"][0]["message"]["content"])
    else:
        logger.error("error body: %s", resp.text[:400])
    assert resp.status_code == 200
