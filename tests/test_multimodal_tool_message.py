"""Send a message history containing a *multimodal tool message* to the model.

The invoice clerk's ``get_invoice`` tool can return a tool message whose content
is a multimodal list — a text block plus an ``image_url`` block carrying a
base64 data URL (see ``src/engine/agents/tools/get_invoice.py``). This test uses
the REAL invoice_clerk system prompt (``src/engine/prompts/invoice_clerk.md``,
rendered as ``PromptCache`` does) and simulates a real ``get_invoice`` return for
an image invoice with an EMPTY summary, then:

  * Section A — payload-level unit tests (no network): asserts the multimodal
    tool message survives ``ChatOpenAI._get_request_payload`` intact (image_url
    block preserved, base64 data URL untouched).
  * Section B — integration test against the LiteLLM proxy from .env: actually
    sends the history and checks the vision model can read the image. We use the
    Nirvana "Nevermind" cover (a downscaled 300x300 JPEG shipped in
    tests/fixtures/) so the model has recognizable detail to describe.

Section B is marked @pytest.mark.integration and skipped when the proxy is
unreachable or the configured model isn't served.
"""

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from src.engine.agents.executor import ChatOpenAI
from src.engine.config import Config
from src.engine.services.prompt_cache import _today_ru

logger = logging.getLogger(__name__)

# --- the test image: Nirvana "Nevermind" cover, read + encoded at runtime -----
_IMAGE_PATH = Path(__file__).parent / "fixtures" / "invoice.jpg"
_IMAGE_B64 = base64.b64encode(_IMAGE_PATH.read_bytes()).decode("ascii")
_IMAGE_DATA_URL = f"data:image/jpeg;base64,{_IMAGE_B64}"

# --- the real invoice clerk system prompt, rendered as production does ---------
_PROMPTS_DIR = Path(__file__).parent.parent / "src" / "engine" / "prompts"
# The invoice_clerk role's tool set (migration 045 + rag tools from 049).
_INVOICE_CLERK_TOOLS = [
    "list_invoices",
    "get_invoice",
    "show_modal",
    "rag_search",
    "table_rows_search",
    "expand_chunk",
]


def _invoice_clerk_system_prompt() -> str:
    """Render src/engine/prompts/invoice_clerk.md exactly like PromptCache does.

    {tools} is filled from prompts/tools/<name>.md joined with "\\n---\\n" (the
    same shape ToolRegistry.resolve produces) and {today} from _today_ru — so the
    model sees the genuine production system prompt, not a paraphrase.
    """
    prompt = (_PROMPTS_DIR / "invoice_clerk.md").read_text(encoding="utf-8")

    doc_parts = []
    for name in _INVOICE_CLERK_TOOLS:
        doc_path = _PROMPTS_DIR / "tools" / f"{name}.md"
        if doc_path.exists():
            doc_parts.append(doc_path.read_text(encoding="utf-8").strip())
    prompt = prompt.replace("{tools}", "\n---\n".join(doc_parts))
    prompt = prompt.replace("{today}", _today_ru())
    return prompt


# A message history ending in a multimodal tool message — exactly what
# get_invoice returns for an *image* invoice with an empty summary: the text
# block omits the "summary:" line (see get_invoice.py: it appends summary only
# when f.summary is truthy), followed by the image_url block. The assistant turn
# that "called" the tool is included so the tool message has a matching
# tool_call_id, mirroring a real LangGraph turn.
TOOL_CALL_ID = "call_invoice_1"
INVOICE_ID = "11111111-1111-1111-1111-111111111111"
FILE_ID = "22222222-2222-2222-2222-222222222222"

# The exact text block get_invoice builds for a pending image invoice with an
# EMPTY summary (no "summary:" / "is_table:" / "rejection_reason:" lines).
_GET_INVOICE_TEXT = "\n".join(
    [
        f"id: {INVOICE_ID}",
        "file: invoice1.jpg",
        f"file_id: {FILE_ID}",
        "status: created",
        "due_date: 2026-07-01",
        "uploader: Иван Петров",
    ]
)


def _history():
    return [
        {"role": "system", "content": _invoice_clerk_system_prompt()},
        {"role": "user", "content": "Покажи счёт и скажи, что на нём изображено."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": TOOL_CALL_ID,
                    "type": "function",
                    "function": {
                        "name": "get_invoice",
                        "arguments": json.dumps({"invoice_id": INVOICE_ID}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": TOOL_CALL_ID,
            # Simulated real get_invoice return: multimodal list, image + empty summary.
            "content": [
                {"type": "text", "text": _GET_INVOICE_TEXT},
                {"type": "image_url", "image_url": {"url": _IMAGE_DATA_URL}},
            ],
        },
    ]


def _user_history():
    """Same image + same real system prompt, but image in a *user* message.

    Control path: if the user-message path describes the image but the
    tool-message path doesn't, the problem is tool-role multimodal handling,
    not the image itself.
    """
    return [
        {"role": "system", "content": _invoice_clerk_system_prompt()},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Покажи счёт и скажи, что на нём изображено."},
                {"type": "image_url", "image_url": {"url": _IMAGE_DATA_URL}},
            ],
        },
    ]


def _make_llm():
    return ChatOpenAI(
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


def test_fixture_image_is_a_real_jpeg():
    """Sanity: the shipped fixture decodes to a non-trivial JPEG."""
    data = _IMAGE_PATH.read_bytes()
    assert data[:3] == b"\xff\xd8\xff", "fixture is not a JPEG"
    assert len(data) > 5_000, "fixture image looks suspiciously small"


def test_multimodal_tool_message_survives_payload_serialization():
    """The image_url block + base64 data URL must reach the wire unmodified."""
    llm = _make_llm()
    payload = llm._get_request_payload(_history())

    tool_msg = next(m for m in payload["messages"] if m.get("role") == "tool")
    content = tool_msg["content"]
    assert isinstance(content, list), "tool message content should stay a list of blocks"

    types = [block.get("type") for block in content]
    assert "text" in types and "image_url" in types, f"missing blocks, got {types}"

    image_block = next(b for b in content if b.get("type") == "image_url")
    assert image_block["image_url"]["url"] == _IMAGE_DATA_URL, "data URL was altered"
    assert image_block["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_tool_message_tool_call_id_is_preserved():
    """tool_call_id must round-trip so the model can match call -> result."""
    llm = _make_llm()
    payload = llm._get_request_payload(_history())
    tool_msg = next(m for m in payload["messages"] if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == TOOL_CALL_ID


# ---------------------------------------------------------------------------
# Section B — integration test (real LiteLLM proxy from .env)
# ---------------------------------------------------------------------------


def _redacted(messages):
    """Copy of the messages with base64 image URLs replaced by a short marker."""
    loggable = json.loads(json.dumps(messages, default=str))
    for msg in loggable:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    block["image_url"]["url"] = f"<data url, {len(url)} chars>"
    return loggable


def _send_and_describe(messages, label: str) -> str:
    """POST the history to the proxy, assert 200, and surface the description.

    Returns the model's reply. The reply is logged AND printed so the model's
    description of the image is visible in the test output (run with ``-s``).
    """
    llm = _make_llm()
    payload = llm._get_request_payload(messages)

    logger.info(
        "[%s] sending to %s model=%s:\n%s",
        label,
        Config.LLM_BASE_URL,
        Config.DEFAULT_MODEL,
        json.dumps(_redacted(payload["messages"]), indent=2, default=str),
    )

    resp = httpx.post(
        f"{Config.LLM_BASE_URL}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {Config.LLM_API_KEY}"},
        timeout=120,
    )
    logger.info("[%s] response status: %d", label, resp.status_code)
    if resp.status_code != 200:
        logger.error("[%s] error body: %s", label, resp.text[:600])
    assert resp.status_code == 200, resp.text[:600]

    reply = resp.json()["choices"][0]["message"]["content"]
    logger.info("[%s] model reply: %s", label, reply)
    print(f"\n=== model description via {label} ===\n{reply}\n")
    assert reply and reply.strip(), "model returned empty content for the image"
    return reply


@integration
def test_integration_model_reads_image_from_multimodal_tool_message():
    """Image carried inside a *tool* message — the get_invoice shape.

    A 200 with non-empty content proves the vision model accepted a base64 image
    delivered through a tool-role message (not just a user message). The model's
    description is printed so you can confirm it actually recognized the cover.
    """
    _send_and_describe(_history(), "tool multimodal message")


@integration
def test_integration_model_reads_image_from_multimodal_user_message():
    """Control: same image carried inside a *user* multimodal message.

    Printed alongside the tool-message description so the two paths can be
    compared directly.
    """
    _send_and_describe(_user_history(), "user multimodal message")
