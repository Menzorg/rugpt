"""
Unit tests: parallel tool calls and token budget enforcement.

Each test fires 40 tool calls in a single AIMessage burst (via _BurstLLM).
With critical_tokens_cap=30_000 and ~1k tokens per call output:
  - ~30 calls succeed and contribute tokens
  - ~10 calls are blocked by the anti-burst mechanism

Includes a standalone tokenizer parity test (100 runs) verifying that
tiktoken cl100k_base and the project HuggingFace tokenizer count within 15%.
"""
from __future__ import annotations

import re
import sys
import types
import random
import string
import logging
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock
from uuid import uuid4, UUID

import pytest
from tokenizers import Tokenizer

# ---------------------------------------------------------------------------
# Module stubs — must happen before any tool module is imported
# ---------------------------------------------------------------------------

def _stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules.setdefault(name, m)
    return sys.modules[name]

_ul = _stub("src.engine.unified_logger")
_ul.get_logger = lambda name: logging.getLogger(name)  # type: ignore[attr-defined]

_rag_svc_stub = _stub("src.engine.services.rag_service")
_rag_svc_stub.RAGService = object  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Real imports (after stubs are in place)
# ---------------------------------------------------------------------------

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolRuntime

from src.engine.agents.runtime import RuntimeContext
from src.engine.agents.executor import MAX_CONCURRENCY
from src.engine.models.user_file import UserFile
from src.engine.models.task import Task

# ---------------------------------------------------------------------------
# Tokenizer setup
# ---------------------------------------------------------------------------

from src.engine.utils.token_counter import _encode_with as _encode_engine

_TARGET_TOKENS = 1000
_TOKENIZERS_DIR = Path(__file__).resolve().parents[1] / "tokenizers"


def _get_hf_tokenizer() -> Tokenizer:
    """Load the default model tokenizer from project cache; download tokenizer.json if absent."""
    from src.engine.config import Config
    model_id = Config.DEFAULT_MODEL
    dir_name = re.sub(r"[^a-zA-Z0-9._-]", "_", model_id.split("/")[-1])
    cache_dir = _TOKENIZERS_DIR / dir_name
    tokenizer_file = cache_dir / "tokenizer.json"
    if not tokenizer_file.exists():
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=model_id,
            local_dir=str(cache_dir),
            allow_patterns=["tokenizer.json"],
        )
    return Tokenizer.from_file(str(tokenizer_file))


_HF_TOKENIZER: Tokenizer | None = None


def _hf_count(text: str) -> int:
    global _HF_TOKENIZER
    if _HF_TOKENIZER is None:
        _HF_TOKENIZER = _get_hf_tokenizer()
    return len(_HF_TOKENIZER.encode(text).ids)


_RU_VOCAB = [
    # существительные
    "время", "год", "человек", "день", "рука", "часть", "место", "случай", "неделя", "работа",
    "слово", "жизнь", "дело", "страна", "город", "вопрос", "система", "компания", "проблема",
    "вода", "земля", "свет", "ночь", "утро", "вечер", "дорога", "дом", "окно", "стол", "книга",
    "деньги", "власть", "сила", "мысль", "голос", "ребёнок", "отец", "мать", "друг", "брат",
    "сестра", "муж", "жена", "семья", "народ", "война", "мир", "история", "закон", "право",
    "машина", "школа", "университет", "врач", "учитель", "министр", "президент", "директор",
    "письмо", "телефон", "экран", "компьютер", "программа", "данные", "сервер", "запрос",
    "результат", "задача", "ответ", "решение", "цель", "план", "отчёт", "документ", "файл",
    # глаголы
    "говорить", "знать", "думать", "видеть", "хотеть", "делать", "идти", "стоять", "читать",
    "писать", "работать", "жить", "любить", "понимать", "получить", "дать", "сказать", "стать",
    "найти", "считать", "называть", "помнить", "решить", "открыть", "начать", "выйти", "прийти",
    "принять", "взять", "поставить", "держать", "оставить", "создать", "использовать", "строить",
    # прилагательные
    "новый", "большой", "первый", "старый", "хороший", "маленький", "последний", "молодой",
    "важный", "высокий", "общий", "главный", "следующий", "открытый", "возможный", "нужный",
    "русский", "государственный", "политический", "экономический", "социальный", "военный",
    "белый", "чёрный", "красный", "синий", "зелёный", "быстрый", "медленный", "сильный",
    # наречия и служебные
    "очень", "уже", "ещё", "только", "также", "вот", "даже", "здесь", "потом", "всегда",
    "часто", "иногда", "сейчас", "сегодня", "вчера", "завтра", "там", "тут", "вместе",
    # предлоги и союзы (короткие, создают текстуру)
    "и", "в", "на", "по", "с", "из", "но", "что", "как", "так", "или", "за", "до", "от",
]

_RU_TEMPLATES = [
    "{subj} {verb} {obj}",
    "{adj} {subj} {verb} {adv}",
    "{subj} и {subj} {verb} {obj}",
    "{adv} {verb} {adj} {obj}",
    "{subj} {verb} в {obj}",
    "{adj} {subj} из {obj}",
    "{verb} {subj} на {obj}",
    "{subj} {verb} {obj} и {obj}",
]

_RU_NOUNS = [w for w in _RU_VOCAB if w[0].islower() and len(w) > 4 and w not in {
    "очень", "уже", "ещё", "только", "также", "вот", "даже", "здесь", "потом", "всегда",
    "часто", "иногда", "сейчас", "сегодня", "вчера", "завтра", "там", "тут", "вместе",
    "говорить", "знать", "думать", "видеть", "хотеть", "делать", "идти", "стоять", "читать",
    "писать", "работать", "жить", "любить", "понимать", "получить", "дать", "сказать", "стать",
    "найти", "считать", "называть", "помнить", "решить", "открыть", "начать", "выйти", "прийти",
    "принять", "взять", "поставить", "держать", "оставить", "создать", "использовать", "строить",
    "новый", "большой", "первый", "старый", "хороший", "маленький", "последний", "молодой",
    "важный", "высокий", "общий", "главный", "следующий", "открытый", "возможный", "нужный",
    "русский", "государственный", "политический", "экономический", "социальный", "военный",
    "белый", "чёрный", "красный", "синий", "зелёный", "быстрый", "медленный", "сильный",
}]
_RU_VERBS = [
    "говорить", "знать", "думать", "видеть", "хотеть", "делать", "идти", "стоять", "читать",
    "писать", "работать", "жить", "любить", "понимать", "получить", "дать", "сказать", "стать",
    "найти", "считать", "называть", "помнить", "решить", "открыть", "начать", "выйти", "прийти",
    "принять", "взять", "поставить", "держать", "оставить", "создать", "использовать", "строить",
]
_RU_ADJS = [
    "новый", "большой", "первый", "старый", "хороший", "маленький", "последний", "молодой",
    "важный", "высокий", "общий", "главный", "следующий", "открытый", "возможный", "нужный",
    "русский", "государственный", "политический", "экономический", "социальный", "военный",
    "белый", "чёрный", "красный", "синий", "зелёный", "быстрый", "медленный", "сильный",
]
_RU_ADVS = [
    "очень", "уже", "ещё", "только", "также", "даже", "здесь", "потом", "всегда",
    "часто", "иногда", "сейчас", "сегодня", "вчера", "завтра", "там", "тут", "вместе",
]

_EN_NOUNS = [
    "time", "year", "person", "day", "hand", "part", "place", "case", "week", "company",
    "word", "life", "work", "country", "city", "question", "system", "problem", "water",
    "road", "house", "window", "table", "book", "money", "power", "thought", "voice",
    "child", "father", "mother", "friend", "brother", "sister", "family", "nation",
    "machine", "school", "university", "doctor", "teacher", "president", "director",
    "letter", "phone", "screen", "computer", "program", "data", "server", "request",
    "result", "task", "answer", "solution", "goal", "plan", "report", "document", "file",
]
_EN_VERBS = [
    "say", "know", "think", "see", "want", "make", "go", "stand", "read", "write",
    "work", "live", "love", "understand", "receive", "give", "become", "find", "use",
    "remember", "solve", "open", "start", "leave", "create", "build", "hold", "accept",
]
_EN_ADJS = [
    "new", "big", "first", "old", "good", "small", "last", "young", "important", "high",
    "general", "main", "next", "open", "possible", "necessary", "white", "black", "red",
    "fast", "slow", "strong", "political", "social", "military", "economic", "public",
]
_EN_ADVS = [
    "very", "already", "still", "only", "also", "even", "here", "then", "always",
    "often", "sometimes", "now", "today", "yesterday", "tomorrow", "there", "together",
]

_EN_TEMPLATES = [
    "{subj} {verb} {obj}",
    "{adj} {subj} {verb} {adv}",
    "{subj} and {subj} {verb} {obj}",
    "{adv} {verb} {adj} {obj}",
    "{subj} {verb} in {obj}",
    "{adj} {subj} from {obj}",
    "{verb} {subj} on {obj}",
    "{subj} {verb} {obj} and {obj}",
]


def _make_chunk(seed: int | None = None) -> str:
    """Return a string of ~1000 tokens (as counted by _encode_engine) from random Russian sentences."""
    rng = random.Random(seed)
    sentences: list[str] = []
    while _encode_engine(" ".join(sentences)) < _TARGET_TOKENS:
        tmpl = rng.choice(_RU_TEMPLATES)
        sentence = tmpl.format(
            subj=rng.choice(_RU_NOUNS),
            verb=rng.choice(_RU_VERBS),
            obj=rng.choice(_RU_NOUNS),
            adj=rng.choice(_RU_ADJS),
            adv=rng.choice(_RU_ADVS),
        )
        sentences.append(sentence)
    return " ".join(sentences)


def _make_chunk_latin(seed: int | None = None) -> str:
    """Return a string of ~1000 tokens from random English sentences."""
    rng = random.Random(seed)
    sentences: list[str] = []
    while _encode_engine(" ".join(sentences)) < _TARGET_TOKENS:
        tmpl = rng.choice(_EN_TEMPLATES)
        sentence = tmpl.format(
            subj=rng.choice(_EN_NOUNS),
            verb=rng.choice(_EN_VERBS),
            obj=rng.choice(_EN_NOUNS),
            adj=rng.choice(_EN_ADJS),
            adv=rng.choice(_EN_ADVS),
        )
        sentences.append(sentence)
    return " ".join(sentences)


def _make_chunk_mixed(seed: int | None = None) -> str:
    """Return a string of ~1000 tokens alternating Russian and English sentences."""
    rng = random.Random(seed)
    sentences: list[str] = []
    while _encode_engine(" ".join(sentences)) < _TARGET_TOKENS:
        if rng.random() < 0.5:
            tmpl = rng.choice(_RU_TEMPLATES)
            sentence = tmpl.format(
                subj=rng.choice(_RU_NOUNS),
                verb=rng.choice(_RU_VERBS),
                obj=rng.choice(_RU_NOUNS),
                adj=rng.choice(_RU_ADJS),
                adv=rng.choice(_RU_ADVS),
            )
        else:
            tmpl = rng.choice(_EN_TEMPLATES)
            sentence = tmpl.format(
                subj=rng.choice(_EN_NOUNS),
                verb=rng.choice(_EN_VERBS),
                obj=rng.choice(_EN_NOUNS),
                adj=rng.choice(_EN_ADJS),
                adv=rng.choice(_EN_ADVS),
            )
        sentences.append(sentence)
    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Tokenizer parity test (100 parametrized runs)
# ---------------------------------------------------------------------------

_parity_logger = logging.getLogger("test.tokenizer_parity")


@pytest.mark.parametrize("run", range(10))
def test_tokenizer_parity(run: int) -> None:
    chunk = _make_chunk(seed=run)
    tt_count = _encode_engine(chunk)
    hf_count = _hf_count(chunk)
    max_val = max(tt_count, hf_count)
    delta = abs(tt_count - hf_count)
    allowed_delta = 0.15 * max_val
    deviation_pct = delta / max_val * 100
    _parity_logger.info(
        "[parity seed=%d] tiktoken_engine=%d hf=%d delta=%d deviation=%.1f%% allowed=%.1f%%",
        run, tt_count, hf_count, delta, deviation_pct, allowed_delta / max_val * 100,
    )
    assert delta <= allowed_delta, (
        f"seed={run}: tiktoken_engine={tt_count} hf={hf_count} "
        f"delta={delta:.0f} > {allowed_delta:.0f} ({deviation_pct:.1f}%)"
    )


@pytest.mark.parametrize("run", range(10))
def test_tokenizer_parity_latin(run: int) -> None:
    chunk = _make_chunk_latin(seed=run)
    tt_count = _encode_engine(chunk)
    hf_count = _hf_count(chunk)
    max_val = max(tt_count, hf_count)
    delta = abs(tt_count - hf_count)
    allowed_delta = 0.15 * max_val
    deviation_pct = delta / max_val * 100
    _parity_logger.info(
        "[parity_latin seed=%d] tiktoken_engine=%d hf=%d delta=%d deviation=%.1f%%",
        run, tt_count, hf_count, delta, deviation_pct,
    )
    assert delta <= allowed_delta, (
        f"seed={run}: tiktoken_engine={tt_count} hf={hf_count} "
        f"delta={delta:.0f} > {allowed_delta:.0f} ({deviation_pct:.1f}%)"
    )


@pytest.mark.parametrize("run", range(10))
def test_tokenizer_parity_mixed(run: int) -> None:
    chunk = _make_chunk_mixed(seed=run)
    tt_count = _encode_engine(chunk)
    hf_count = _hf_count(chunk)
    max_val = max(tt_count, hf_count)
    delta = abs(tt_count - hf_count)
    allowed_delta = 0.15 * max_val
    deviation_pct = delta / max_val * 100
    _parity_logger.info(
        "[parity_mixed seed=%d] tiktoken_engine=%d hf=%d delta=%d deviation=%.1f%%",
        run, tt_count, hf_count, delta, deviation_pct,
    )
    assert delta <= allowed_delta, (
        f"seed={run}: tiktoken_engine={tt_count} hf={hf_count} "
        f"delta={delta:.0f} > {allowed_delta:.0f} ({deviation_pct:.1f}%)"
    )


# ---------------------------------------------------------------------------
# Shared agent infrastructure
# ---------------------------------------------------------------------------

_N_CALLS = 40
_CAP = 30_000


class _BurstLLM(BaseChatModel):
    """Fake LLM: first call emits _N_CALLS tool calls at once, second call stops."""

    _tool_name: str = ""
    _calls_args: list = []
    _fired: bool = False

    def __init__(self, tool_name: str, calls_args: list, **kwargs):
        super().__init__(**kwargs)
        object.__setattr__(self, "_tool_name", tool_name)
        object.__setattr__(self, "_calls_args", calls_args)
        object.__setattr__(self, "_fired", False)

    @property
    def _llm_type(self) -> str:
        return "burst"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if not self._fired:
            object.__setattr__(self, "_fired", True)
            tool_calls = [
                {
                    "name": self._tool_name,
                    "args": a,
                    "id": f"tc-{i}",
                    "type": "tool_call",
                }
                for i, a in enumerate(self._calls_args)
            ]
            msg = AIMessage(content="", tool_calls=tool_calls)
        else:
            msg = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator:
        yield from []


def _config(org_id: UUID, caller_id: UUID) -> RunnableConfig:
    return RunnableConfig(
        max_concurrency=MAX_CONCURRENCY,
        configurable={
            "org_id": str(org_id),
            "caller_user_id": str(caller_id),
            "callee_user_id": str(caller_id),
            "invocation_kind": "direct",
            "is_admin": False,
            "timezone": "Europe/Moscow",
            "chat_id": None,
        },
    )


def _tool_outputs(result: dict) -> list[str]:
    return [m.content for m in result["messages"] if isinstance(m, ToolMessage)]


def _log_budget_results(
    test_name: str,
    runtime_ctx: RuntimeContext,
    outputs: list[str],
    blocked_marker: tuple[str, ...] = (),
) -> None:
    """Log token budget results with counts from both tokenizers."""
    total_text = "\n".join(outputs)
    tt_count = _encode_engine(total_text)   # token_counter._encode (tiktoken * 0.56)
    hf_count = _hf_count(total_text)
    deviation_pct = abs(tt_count - hf_count) / max(tt_count, hf_count) * 100

    blocked = (
        sum(1 for o in outputs if any(m in o for m in blocked_marker))
        if blocked_marker else 0
    )
    full = len(outputs) - blocked

    logging.getLogger("test.token_budget").info(
        "[%s] total_tokens_spent=%d cap=%d | outputs=%d full=%d blocked=%d | "
        "tiktoken=%d hf=%d deviation=%.1f%%",
        test_name,
        runtime_ctx.total_tokens_spent,
        runtime_ctx.critical_tokens_cap,
        len(outputs),
        full,
        blocked,
        tt_count,
        hf_count,
        deviation_pct,
    )


# ---------------------------------------------------------------------------
# Test 1: rag_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rag_search_parallel_token_budget(monkeypatch):
    import src.engine.agents.tools.rag_tool as rag_mod

    org_id = uuid4()
    caller_id = uuid4()
    file_uuid = uuid4()

    fake_file = UserFile(
        id=file_uuid,
        user_id=caller_id,
        org_id=org_id,
        uploaded_by_user_id=caller_id,
        original_filename="test.pdf",
        file_type="pdf",
        rag_status="indexed",
        is_public=True,
    )

    call_counter = {"n": 0}

    async def _search(file_id, query, top_k=4):
        i = call_counter["n"]
        call_counter["n"] += 1
        return [SimpleNamespace(
            chunk_id=uuid4(),
            chunk_index=i,
            chunk_text=_make_chunk(i),
            source_type="text",
        )]

    async def _get_by_id(fid):
        return fake_file if fid == file_uuid else None

    async def _get_status(fid):
        return "indexed"

    async def _list_by_org(oid):
        return [fake_file]

    fake_rag = SimpleNamespace(search_concrete_in_doc=_search)
    fake_file_storage = SimpleNamespace(
        get_by_id=_get_by_id,
        get_status=_get_status,
        list_by_org=_list_by_org,
    )

    monkeypatch.setattr(rag_mod, "_rag_service", fake_rag)
    monkeypatch.setattr(rag_mod, "_user_file_storage", fake_file_storage)
    monkeypatch.setattr(rag_mod, "_chat_storage", None)

    from src.engine.agents.tools.rag_tool import rag_search

    calls_args = [{"file_id": str(file_uuid), "query": f"query_{i}"} for i in range(_N_CALLS)]
    llm = _BurstLLM(tool_name="rag_search", calls_args=calls_args)
    runtime_ctx = RuntimeContext(critical_tokens_cap=_CAP)
    agent = create_agent(llm, tools=[rag_search], context_schema=RuntimeContext)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "use all tools"}]},
        config=_config(org_id, caller_id),
        context=runtime_ctx,
    )

    outputs = _tool_outputs(result)
    assert len(outputs) == _N_CALLS, f"Expected {_N_CALLS} tool responses, got {len(outputs)}"

    _BLOCKED = ("BLOCKED", "CONTEXT WINDOW EXPLOSION")
    _log_budget_results("rag_search", runtime_ctx, outputs, blocked_marker=_BLOCKED)
    blocked = [o for o in outputs if any(m in o for m in _BLOCKED)]
    full = [o for o in outputs if not any(m in o for m in _BLOCKED)]

    assert 28 < len(full) < 32, f"Expected ~30 full responses, got {len(full)}"
    assert 8 < len(blocked) < 12, f"Expected ~10 blocked responses, got {len(blocked)}"
    assert runtime_ctx.total_tokens_spent <= _CAP + 6000, (
        f"Token overflow: {runtime_ctx.total_tokens_spent} > {_CAP + 6000}"
    )


# ---------------------------------------------------------------------------
# Test 2: task_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_query_parallel_token_budget(monkeypatch):
    # task_query uses `config: RunnableConfig = None` (not injected by the agent framework
    # because of the default), so we simulate parallelism directly via asyncio + semaphore,
    # matching the MAX_CONCURRENCY behaviour of the real executor.
    org_id = uuid4()
    caller_id = uuid4()
    assignee_id = uuid4()

    task_call_counter = {"n": 0}
    task_service = SimpleNamespace()

    async def _list_by_org(qorg_id, status=None):
        i = task_call_counter["n"]
        task_call_counter["n"] += 1
        return [Task(
            org_id=org_id,
            title=f"Task {i}",
            description=_make_chunk(i),
            status="created",
            assignee_user_id=assignee_id,
            priority=1,
        )]

    task_service.list_by_org = _list_by_org
    task_service.list_by_assignee = AsyncMock(return_value=[])
    task_service.list_tasks_created_by = AsyncMock(return_value=[])
    task_service.text_search = AsyncMock(return_value=[])
    task_service.list_by_deadline_range = AsyncMock(return_value=[])
    task_service.list_by_created_range = AsyncMock(return_value=[])

    engine_ns = SimpleNamespace(
        department_service=SimpleNamespace(
            get_visible_user_ids=AsyncMock(return_value={assignee_id}),
        ),
        user_storage=SimpleNamespace(
            get_certain_users=AsyncMock(
                return_value=[SimpleNamespace(id=assignee_id, name="User")]
            ),
        ),
        task_participant_storage=SimpleNamespace(
            get_for_tasks=AsyncMock(return_value={}),
        ),
    )
    monkeypatch.setattr(
        "src.engine.services.engine_service.get_engine_service",
        lambda: engine_ns,
    )

    from src.engine.agents.tools.task_tool import create_task_tools
    _create, query_tool, *_rest = create_task_tools(task_service)

    cfg = {
        "configurable": {
            "org_id": str(org_id),
            "caller_user_id": str(caller_id),
            "callee_user_id": str(caller_id),
            "invocation_kind": "direct",
            "is_admin": False,
            "timezone": "Europe/Moscow",
            "chat_id": None,
        }
    }
    runtime_ctx = RuntimeContext(critical_tokens_cap=_CAP)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    def _make_runtime() -> ToolRuntime:
        return ToolRuntime(
            state=None,
            context=runtime_ctx,
            config=cfg,
            stream_writer=lambda *a, **kw: None,
            tool_call_id=None,
            store=None,
        )

    async def _call(_i: int) -> str:
        async with sem:
            return await query_tool.coroutine(config=cfg, runtime=_make_runtime())

    outputs = list(await asyncio.gather(*[_call(i) for i in range(_N_CALLS)]))

    _log_budget_results("task_query", runtime_ctx, outputs)
    hidden = [o for o in outputs if "TASK DESCRIPTIONS HIDDEN" in o]
    assert 8 <= len(hidden) <= 15, (
        f"Expected 8-15 description-hidden responses (depending on concurrency timing), got {len(hidden)}"
    )
    assert runtime_ctx.total_tokens_spent <= _CAP + 6000, (
        f"Token overflow: {runtime_ctx.total_tokens_spent} > {_CAP + 6000}"
    )


# ---------------------------------------------------------------------------
# Test 3: list_documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_documents_parallel_token_budget(monkeypatch):
    import src.engine.agents.tools.list_documents as ldoc_mod

    org_id = uuid4()
    caller_id = uuid4()

    files = [
        UserFile(
            id=uuid4(),
            user_id=caller_id,
            org_id=org_id,
            uploaded_by_user_id=caller_id,
            original_filename=f"doc_{i}.pdf",
            file_type="pdf",
            rag_status="indexed",
            is_public=True,
            summary=_make_chunk(i),
        )
        for i in range(40)
    ]
    files_by_id = {f.id: f for f in files}

    async def _list_by_org(oid):
        return files

    async def _get_by_id(fid):
        return files_by_id.get(fid)

    fake_file_storage = SimpleNamespace(
        list_by_org=_list_by_org,
        get_by_id=_get_by_id,
    )

    monkeypatch.setattr(ldoc_mod, "_user_file_storage", fake_file_storage)
    monkeypatch.setattr(ldoc_mod, "_user_storage", None)
    monkeypatch.setattr(ldoc_mod, "_rag_service", None)
    monkeypatch.setattr(ldoc_mod, "_chat_storage", None)

    from src.engine.agents.tools.list_documents import list_documents

    calls_args = [{} for _ in range(_N_CALLS)]
    llm = _BurstLLM(tool_name="list_documents", calls_args=calls_args)
    runtime_ctx = RuntimeContext(critical_tokens_cap=_CAP)
    agent = create_agent(llm, tools=[list_documents], context_schema=RuntimeContext)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "use all tools"}]},
        config=_config(org_id, caller_id),
        context=runtime_ctx,
    )

    outputs = _tool_outputs(result)
    assert len(outputs) == _N_CALLS, f"Expected {_N_CALLS} tool responses, got {len(outputs)}"

    _log_budget_results("list_documents", runtime_ctx, outputs)
    # list_documents enforces summary budget within the first call (proportional truncation).
    # Subsequent parallel calls hit dedup (all same files already seen) and return short messages.
    # The first call includes truncated summaries (shown as "word..."); later calls are short dedup msgs.

    # At least one full response must exist (the first call)
    full_responses = [o for o in outputs if len(o) > 200]
    assert len(full_responses) >= 1, "Expected at least one full list_documents response"

    # First full response should contain at least one truncated summary (ends with "...")
    first_full = full_responses[0]
    assert "..." in first_full, (
        f"Expected summary truncation in first full response (summaries cut to fit budget):\n{first_full[:400]}"
    )

    # Most calls (dedup): 40 calls × same page 1 → all but first(s) return short dedup/blocked messages
    dedup_responses = [o for o in outputs if "already shown" in o.lower() or "No documents" in o or "BLOCKED" in o]
    assert len(dedup_responses) >= 28, (
        f"Expected >=28 dedup responses after first call, got {len(dedup_responses)}"
    )

    assert runtime_ctx.total_tokens_spent <= _CAP + 6000, (
        f"Token overflow: {runtime_ctx.total_tokens_spent} > {_CAP + 6000}"
    )


# ---------------------------------------------------------------------------
# Test 4: table_rows_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_table_rows_parallel_token_budget(monkeypatch):
    import src.engine.agents.tools.table_rows_tool as trt_mod

    org_id = uuid4()
    caller_id = uuid4()
    file_uuid = uuid4()

    fake_file = UserFile(
        id=file_uuid,
        user_id=caller_id,
        org_id=org_id,
        uploaded_by_user_id=caller_id,
        original_filename="table.csv",
        file_type="csv",
        rag_status="indexed",
        is_public=True,
    )

    row_call_counter = {"n": 0}

    async def _get_rows(file_id, row_start, row_end):
        i = row_call_counter["n"]
        row_call_counter["n"] += 1
        return [SimpleNamespace(chunk_text=_make_chunk(i))]

    async def _get_by_id(fid):
        return fake_file if fid == file_uuid else None

    async def _list_by_org(oid):
        return [fake_file]

    fake_rag = SimpleNamespace(get_table_rows_by_range=_get_rows)
    fake_file_storage = SimpleNamespace(
        get_by_id=_get_by_id,
        list_by_org=_list_by_org,
    )

    monkeypatch.setattr(trt_mod, "_rag_service", fake_rag)
    monkeypatch.setattr(trt_mod, "_user_file_storage", fake_file_storage)
    monkeypatch.setattr(trt_mod, "_chat_storage", None)

    from src.engine.agents.tools.table_rows_tool import table_rows_search

    calls_args = [
        {"file_id": str(file_uuid), "row_start": i, "row_end": i}
        for i in range(_N_CALLS)
    ]
    llm = _BurstLLM(tool_name="table_rows_search", calls_args=calls_args)
    runtime_ctx = RuntimeContext(critical_tokens_cap=_CAP)
    agent = create_agent(llm, tools=[table_rows_search], context_schema=RuntimeContext)

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "use all tools"}]},
        config=_config(org_id, caller_id),
        context=runtime_ctx,
    )

    outputs = _tool_outputs(result)
    assert len(outputs) == _N_CALLS, f"Expected {_N_CALLS} tool responses, got {len(outputs)}"

    _BLOCKED = ("BLOCKED", "CONTEXT WINDOW EXPLOSION")
    _log_budget_results("table_rows_search", runtime_ctx, outputs, blocked_marker=_BLOCKED)
    blocked = [o for o in outputs if any(m in o for m in _BLOCKED)]
    full = [o for o in outputs if not any(m in o for m in _BLOCKED)]

    assert 28 < len(full) < 32, f"Expected ~30 full responses, got {len(full)}"
    assert 8 < len(blocked) < 12, f"Expected ~10 blocked responses, got {len(blocked)}"
    assert runtime_ctx.total_tokens_spent <= _CAP + 6000, (
        f"Token overflow: {runtime_ctx.total_tokens_spent} > {_CAP + 6000}"
    )
