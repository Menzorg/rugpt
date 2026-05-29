# LiteLLM Migration Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to go through tasks sequentially. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить прямые клиенты Ollama (`langchain-ollama`) на OpenAI-совместимый клиент (`langchain-openai`), направленный через LiteLLM proxy на `http://192.168.1.80:4000/v1`. Все вызовы (генерация + эмбеддинги) идут через единый шлюз, LiteLLM роутит на Ollama/vLLM по имени модели.

**Architecture:** Big-bang replace. Один набор правок, один деплой, откат через git revert. Пакет `langchain-ollama` удаляется из зависимостей. База моделей приводится к именам которые LiteLLM принимает (`google/gemma-4-31B-it`, `Qwen/Qwen3-Embedding-0.6B`).

**Tech Stack:** Python 3.10+, `langchain-openai>=0.2.0`, `langchain-core`, FastAPI, PostgreSQL+asyncpg, pgvector.

**Спецификация:** `docs/superpowers/specs/2026-04-20-litellm-migration-design.md`

**Важное правило:** Порядок задач выбран так, чтобы после **каждой задачи** `./venv/bin/python -c "import src.engine.app"` импорт проходил (кроме Tasks 7-8, где сигнатура RAGService меняется в двух шагах — это прямо отмечено). Деплой выполняется только после Task 10.

**Git-дисциплина:** Пользователь коммитит сам когда готов. Этот план шаги `git add` / `git commit` не содержит.

---

## File Structure

**Создаются:**
- `src/engine/migrations/020_litellm_model_names.sql` — миграция БД

**Редактируются:**
- `requirements.txt` — замена `langchain-ollama` на `langchain-openai`
- `.env.example` — добавление `LLM_API_KEY`, новые дефолты `LLM_BASE_URL`/`DEFAULT_MODEL`/`EMBEDDING_MODEL`
- `src/engine/config.py` — те же новые настройки в коде
- `src/engine/agents/executor.py` — `ChatOllama` → `ChatOpenAI` + `api_key`
- `src/engine/agents/graphs/simple.py` — type hints
- `src/engine/agents/graphs/chain.py` — type hints
- `src/engine/agents/graphs/multi_agent.py` — type hints
- `src/engine/agents/graphs/rule_generator.py` — создание LLM
- `src/engine/services/rag_service.py` — оба клиента + смена параметров `__init__`
- `src/engine/agents/tools/rag_tool.py` — `OllamaEmbeddings` → `OpenAIEmbeddings`
- `src/engine/services/engine_service.py` — удаление OllamaProvider + передача новых параметров в RAGService и AgentExecutor
- `src/engine/tasks/ingest_queue.py` — передача новых параметров в RAGService
- `src/engine/routes/health.py` — замена Ollama health check на LiteLLM

**Удаляются:**
- `src/engine/llm/providers/ollama.py` — legacy
- `src/engine/llm/providers/__init__.py`, `src/engine/llm/providers/base.py`, `src/engine/llm/__init__.py` — если после удаления ollama.py в папке ничего не остаётся

---

## Task 1: Миграция БД 020

**Files:**
- Create: `src/engine/migrations/020_litellm_model_names.sql`

- [ ] **Step 1.1: Создать файл миграции**

Запиши в `src/engine/migrations/020_litellm_model_names.sql`:

```sql
-- Migration 020: Migrate role.model_name to LiteLLM canonical names.
-- All generation roles use Gemma-4 via vLLM through LiteLLM proxy.

UPDATE roles
SET model_name = 'google/gemma-4-31B-it',
    updated_at = NOW()
WHERE model_name IN ('qwen3:14b', 'gpt-oss:20b', 'qwen2.5:7b', 'glm-4.7-flash', 'qwen2:0.5b');
```

- [ ] **Step 1.2: Проверить синтаксис SQL**

Run: `cat src/engine/migrations/020_litellm_model_names.sql | head -15`
Expected: вывод показывает содержимое без ошибок чтения.

---

## Task 2: requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 2.1: Найти строку с langchain-ollama**

Run: `grep -n "langchain-ollama\|langchain-openai" requirements.txt`
Expected: `langchain-ollama>=0.2.0` есть, `langchain-openai` отсутствует.

- [ ] **Step 2.2: Заменить langchain-ollama на langchain-openai**

В `requirements.txt` строку
```
langchain-ollama>=0.2.0
```
заменить на
```
langchain-openai>=0.2.0
```

- [ ] **Step 2.3: Проверить замену**

Run: `grep -n "langchain-ollama\|langchain-openai" requirements.txt`
Expected: только `langchain-openai>=0.2.0`, `langchain-ollama` отсутствует.

---

## Task 3: .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 3.1: Посмотреть текущие LLM-переменные**

Run: `grep -n "LLM_BASE_URL\|DEFAULT_MODEL\|EMBEDDING_MODEL\|OPENAI_API_KEY" .env.example`
Expected: видишь строки `LLM_BASE_URL=http://localhost:11434`, `DEFAULT_MODEL=qwen2.5:7b`, `EMBEDDING_MODEL=qwen3-embedding:0.6b` (имена могут отличаться — важно запомнить исходные значения).

- [ ] **Step 3.2: Обновить LLM-блок в .env.example**

Найди блок c `LLM_BASE_URL=` и замени на:
```
LLM_BASE_URL=http://192.168.1.80:4000/v1
LLM_API_KEY=sk-dummy
DEFAULT_MODEL=google/gemma-4-31B-it
```

Найди строку `EMBEDDING_MODEL=` и замени значение на `Qwen/Qwen3-Embedding-0.6B`:
```
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
```

- [ ] **Step 3.3: Проверить замены**

Run: `grep -n "LLM_BASE_URL\|LLM_API_KEY\|DEFAULT_MODEL\|EMBEDDING_MODEL" .env.example`
Expected:
```
LLM_BASE_URL=http://192.168.1.80:4000/v1
LLM_API_KEY=sk-dummy
DEFAULT_MODEL=google/gemma-4-31B-it
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
```

---

## Task 4: config.py

**Files:**
- Modify: `src/engine/config.py` (строки ~63-69, блок `# LLM settings`)

- [ ] **Step 4.1: Заменить LLM-блок**

В `src/engine/config.py` найди блок:
```python
    # LLM settings
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")  # Ollama default
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2:0.5b")

    # OpenAI fallback (optional)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
```

и замени на:
```python
    # LLM settings — all inference (generation + embeddings) goes through a
    # single OpenAI-compatible gateway (LiteLLM proxy on Zver). LiteLLM itself
    # fans out to Ollama / vLLM behind the scenes based on model name.
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://192.168.1.80:4000/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-dummy")
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "google/gemma-4-31B-it")

    # Legacy OpenAI fields kept as aliases — some older code paths may still
    # read them, but new code should use LLM_* above.
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", LLM_API_KEY)
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
```

- [ ] **Step 4.2: Обновить EMBEDDING_MODEL дефолт**

Найди в `config.py` строку:
```python
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
```
и замени на:
```python
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
```

- [ ] **Step 4.3: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.config import Config; print(Config.LLM_BASE_URL, Config.LLM_API_KEY, Config.DEFAULT_MODEL, Config.EMBEDDING_MODEL)"`
Expected: `http://192.168.1.80:4000/v1 sk-dummy google/gemma-4-31B-it Qwen/Qwen3-Embedding-0.6B`

---

## Task 5: Установить langchain-openai в venv

**Files:** (ничего не меняем, только `pip install`)

- [ ] **Step 5.1: Поставить пакет**

Run: `cd /root/rugpt && ./venv/bin/pip install "langchain-openai>=0.2.0"`
Expected: пакет установлен без ошибок.

- [ ] **Step 5.2: Проверить импорт**

Run: `cd /root/rugpt && ./venv/bin/python -c "from langchain_openai import ChatOpenAI, OpenAIEmbeddings; print('ok')"`
Expected: `ok`.

(На проде `langchain-openai` поставится через `./deploy.sh` когда он увидит изменение в `requirements.txt` и запустит pip — см. Task 13.)

---

## Task 6: graphs/simple.py, chain.py, multi_agent.py — type hints

**Files:**
- Modify: `src/engine/agents/graphs/simple.py`
- Modify: `src/engine/agents/graphs/chain.py`
- Modify: `src/engine/agents/graphs/multi_agent.py`

Эти три файла используют `ChatOllama` только как тип-хинт (не создают инстансы). Меняем только импорт и имя класса в аннотациях.

- [ ] **Step 6.1: simple.py — импорт**

В `src/engine/agents/graphs/simple.py` строку
```python
from langchain_ollama import ChatOllama
```
заменить на
```python
from langchain_openai import ChatOpenAI
```

- [ ] **Step 6.2: simple.py — тип-хинты**

В том же файле все вхождения `ChatOllama` в тип-аннотациях заменить на `ChatOpenAI` (команда `sed`-style мысленная: `ChatOllama` → `ChatOpenAI`, во всём файле).

Проверить: `grep -n "ChatOllama" src/engine/agents/graphs/simple.py` — не должен ничего находить.

- [ ] **Step 6.3: chain.py — те же два изменения**

Заменить `from langchain_ollama import ChatOllama` → `from langchain_openai import ChatOpenAI`; заменить все `ChatOllama` → `ChatOpenAI`.

Проверить: `grep -n "ChatOllama" src/engine/agents/graphs/chain.py` — пусто.

- [ ] **Step 6.4: multi_agent.py — те же два изменения**

Заменить `from langchain_ollama import ChatOllama` → `from langchain_openai import ChatOpenAI`; заменить все `ChatOllama` → `ChatOpenAI`.

Проверить: `grep -n "ChatOllama" src/engine/agents/graphs/multi_agent.py` — пусто.

- [ ] **Step 6.5: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.graphs.simple import run_simple_agent; from src.engine.agents.graphs.chain import run_chain_agent; from src.engine.agents.graphs.multi_agent import run_multi_agent; print('ok')"`
Expected: `ok`.

---

## Task 7: graphs/rule_generator.py

**Files:**
- Modify: `src/engine/agents/graphs/rule_generator.py`

- [ ] **Step 7.1: Заменить импорт**

Строку
```python
from langchain_ollama import ChatOllama
```
заменить на
```python
from langchain_openai import ChatOpenAI

from ...config import Config
```

- [ ] **Step 7.2: Заменить создание LLM**

Блок
```python
    llm = ChatOllama(
        base_url=base_url,
        model=model,
        temperature=temperature,
    )
```
заменить на
```python
    llm = ChatOpenAI(
        base_url=base_url,
        api_key=Config.LLM_API_KEY,
        model=model,
        temperature=temperature,
    )
```

- [ ] **Step 7.3: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.graphs.rule_generator import generate_rule_text; print('ok')"`
Expected: `ok`.

---

## Task 8: agents/executor.py

**Files:**
- Modify: `src/engine/agents/executor.py`

- [ ] **Step 8.1: Заменить импорт**

Строку
```python
from langchain_ollama import ChatOllama
```
заменить на:
```python
from langchain_openai import ChatOpenAI

from ..config import Config
```

- [ ] **Step 8.2: Обновить `__init__` сигнатуру**

Блок
```python
    def __init__(
        self,
        base_url: str,
        default_model: str,
        prompt_cache: PromptCache,
        tool_registry: Optional[ToolRegistry] = None,
        timeout: float = 300.0,
    ):
        self.base_url = base_url
        self.default_model = default_model
        self.prompt_cache = prompt_cache
        self.tool_registry = tool_registry or ToolRegistry()
        self.timeout = timeout
```
заменить на
```python
    def __init__(
        self,
        base_url: str,
        default_model: str,
        prompt_cache: PromptCache,
        tool_registry: Optional[ToolRegistry] = None,
        timeout: float = 300.0,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url
        self.default_model = default_model
        self.prompt_cache = prompt_cache
        self.tool_registry = tool_registry or ToolRegistry()
        self.timeout = timeout
        self.api_key = api_key or Config.LLM_API_KEY
```

- [ ] **Step 8.3: Обновить `_create_llm`**

Блок
```python
    def _create_llm(self, model: str, temperature: float = 0.7) -> ChatOllama:
        """Create a ChatOllama instance for the given model"""
        return ChatOllama(
            base_url=self.base_url,
            model=model,
            temperature=temperature,
            # Ollama-specific timeout handled via request_timeout
        )
```
заменить на
```python
    def _create_llm(self, model: str, temperature: float = 0.7) -> ChatOpenAI:
        """Create a ChatOpenAI instance pointed at the LiteLLM proxy."""
        return ChatOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            model=model,
            temperature=temperature,
            timeout=self.timeout,
        )
```

- [ ] **Step 8.4: Проверить нет ли оставшихся ChatOllama**

Run: `grep -n "ChatOllama" src/engine/agents/executor.py`
Expected: пусто.

- [ ] **Step 8.5: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.executor import AgentExecutor; print('ok')"`
Expected: `ok`.

---

## Task 9: agents/tools/rag_tool.py

**Files:**
- Modify: `src/engine/agents/tools/rag_tool.py`

- [ ] **Step 9.1: Заменить импорт**

Строку
```python
from langchain_ollama.embeddings import OllamaEmbeddings
```
заменить на
```python
from langchain_openai import OpenAIEmbeddings
```

- [ ] **Step 9.2: Заменить создание embeddings**

Блок
```python
def _get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=Config.EMBEDDING_MODEL,
        base_url=Config.LLM_BASE_URL,
    )
```
заменить на
```python
def _get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=Config.EMBEDDING_MODEL,
        base_url=Config.LLM_BASE_URL,
        api_key=Config.LLM_API_KEY,
    )
```

- [ ] **Step 9.3: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.tools.rag_tool import rag_search, init_rag_pool; print('ok')"`
Expected: `ok`.

---

## Task 10: services/rag_service.py (меняется сигнатура __init__)

**Files:**
- Modify: `src/engine/services/rag_service.py`

> **Внимание:** после этой задачи вызовы `RAGService(...)` в `engine_service.py` и `tasks/ingest_queue.py` **сломаны** — старые имена параметров больше не существуют. Tasks 11 и 12 это чинят.

- [ ] **Step 10.1: Заменить импорт**

Строку
```python
from langchain_ollama import ChatOllama, OllamaEmbeddings
```
заменить на
```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
```

- [ ] **Step 10.2: Обновить `__init__` сигнатуру**

Блок (примерно строки 51-62)
```python
    def __init__(
        self,
        store: RAG_store | None = None,
        *,
        ollama_model: str,
        ollama_embeddings_base_url: str,
        ollama_base_url: str,
        chunk_size: int,
        chunk_overlap: int,
        summary_input_max_chars: int,
        file_storage: UserFileStorage | None = None,
    ) -> None:
```
заменить на
```python
    def __init__(
        self,
        store: RAG_store | None = None,
        *,
        embedding_model: str,
        llm_base_url: str,
        llm_api_key: str,
        chunk_size: int,
        chunk_overlap: int,
        summary_input_max_chars: int,
        file_storage: UserFileStorage | None = None,
    ) -> None:
```

- [ ] **Step 10.3: Обновить создание клиентов внутри `__init__`**

Блок
```python
        self._embeddings = OllamaEmbeddings(
            model=ollama_model,
            base_url=ollama_embeddings_base_url,
        )
        self._summary_llm = ChatOllama(  # type: ignore[call-arg]
            model=Config.RAG_SUMMARY_MODEL,
            base_url=ollama_base_url,
            temperature=0,
        )
```
заменить на
```python
        self._embeddings = OpenAIEmbeddings(
            model=embedding_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )
        self._summary_llm = ChatOpenAI(
            model=Config.RAG_SUMMARY_MODEL,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0,
        )
```

- [ ] **Step 10.4: Проверить нет ли оставшихся Ollama-ссылок**

Run: `grep -n "OllamaEmbeddings\|ChatOllama\|ollama_model\|ollama_base_url\|ollama_embeddings_base_url" src/engine/services/rag_service.py`
Expected: пусто.

> Импорт-чек пропускаем: файл импортируется нормально, но вызовы `RAGService(...)` в других местах ещё со старыми именами параметров — будут падать в runtime. Исправим в Tasks 11, 12.

---

## Task 11: services/engine_service.py

**Files:**
- Modify: `src/engine/services/engine_service.py`

Три изменения: убрать импорт/использование `OllamaProvider`, обновить вызов `RAGService(...)`, добавить `api_key` в `AgentExecutor(...)`.

- [ ] **Step 11.1: Найти и посмотреть текущие места**

Run: `grep -n "OllamaProvider\|ollama_model\|ollama_embeddings_base_url\|ollama_base_url\|RAGService(\|AgentExecutor(" src/engine/services/engine_service.py`
Expected: видишь строки с `OllamaProvider`, `RAGService(...)` и `AgentExecutor(...)` — запомни номера строк.

- [ ] **Step 11.2: Убрать импорт OllamaProvider**

В верхней части файла найти импорт вида
```python
from ..llm.providers.ollama import OllamaProvider
```
и **удалить эту строку целиком**.

- [ ] **Step 11.3: Убрать использование OllamaProvider**

Найди в `engine_service.py` строки, создающие и хранящие `OllamaProvider`:
```python
        self.llm_provider = OllamaProvider(...)
```
(или похожее — может быть с `base_url=` параметром).

**Удалить эти строки**. Также если где-то в коде `engine.llm_provider` читается — удалить такие обращения (обычно их нет, провайдер использовался только для health check).

- [ ] **Step 11.4: Обновить создание RAGService**

Найди блок (примерно около строк 178-184):
```python
        self.rag_service = RAGService(
            ...
            ollama_model=Config.EMBEDDING_MODEL,
            ollama_embeddings_base_url=Config.LLM_BASE_URL,
            ollama_base_url=Config.LLM_BASE_URL,
            ...
        )
```
Замени три параметра (`ollama_model`, `ollama_embeddings_base_url`, `ollama_base_url`) на два новых:
```python
        self.rag_service = RAGService(
            ...
            embedding_model=Config.EMBEDDING_MODEL,
            llm_base_url=Config.LLM_BASE_URL,
            llm_api_key=Config.LLM_API_KEY,
            ...
        )
```

Остальные параметры (`store=`, `chunk_size=`, `chunk_overlap=`, `summary_input_max_chars=`, `file_storage=`) не трогай.

- [ ] **Step 11.5: Обновить создание AgentExecutor**

Найди блок (примерно около строки 240):
```python
        self.agent_executor = AgentExecutor(
            base_url=Config.LLM_BASE_URL,
            default_model=Config.DEFAULT_MODEL,
            prompt_cache=self.prompt_cache,
            tool_registry=self.tool_registry,
        )
```

Добавь параметр `api_key=Config.LLM_API_KEY`:
```python
        self.agent_executor = AgentExecutor(
            base_url=Config.LLM_BASE_URL,
            api_key=Config.LLM_API_KEY,
            default_model=Config.DEFAULT_MODEL,
            prompt_cache=self.prompt_cache,
            tool_registry=self.tool_registry,
        )
```

- [ ] **Step 11.6: Проверить нет ли оставшихся старых имён**

Run: `grep -n "OllamaProvider\|ollama_model\|ollama_embeddings_base_url\|ollama_base_url" src/engine/services/engine_service.py`
Expected: пусто.

- [ ] **Step 11.7: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.services.engine_service import get_engine_service, init_engine_service; print('ok')"`
Expected: `ok`.

---

## Task 12: tasks/ingest_queue.py

**Files:**
- Modify: `src/engine/tasks/ingest_queue.py`

- [ ] **Step 12.1: Найти создание RAGService**

Run: `grep -n "ollama_model\|ollama_base_url\|ollama_embeddings_base_url\|RAGService(" src/engine/tasks/ingest_queue.py`
Expected: видишь блок с `RAGService(...)` и тремя `ollama_*=` параметрами.

- [ ] **Step 12.2: Заменить параметры**

Найди блок (примерно строки 50-57):
```python
        rag_service = RAGService(
            ...
            ollama_model=Config.EMBEDDING_MODEL,
            ollama_embeddings_base_url=Config.LLM_BASE_URL,
            ollama_base_url=Config.LLM_BASE_URL,
            ...
        )
```
Замени на:
```python
        rag_service = RAGService(
            ...
            embedding_model=Config.EMBEDDING_MODEL,
            llm_base_url=Config.LLM_BASE_URL,
            llm_api_key=Config.LLM_API_KEY,
            ...
        )
```
(остальные параметры сохраняются).

- [ ] **Step 12.3: Проверить нет ли оставшихся старых имён**

Run: `grep -n "ollama_model\|ollama_base_url\|ollama_embeddings_base_url" src/engine/tasks/ingest_queue.py`
Expected: пусто.

- [ ] **Step 12.4: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.tasks.ingest_queue import ingest_queue; print('ok')"`
Expected: `ok`.

---

## Task 13: routes/health.py — заменить Ollama на LiteLLM health check

**Files:**
- Modify: `src/engine/routes/health.py`

- [ ] **Step 13.1: Посмотреть текущий код**

Run: `cat src/engine/routes/health.py`

Найди места где используется `OllamaProvider` или `llm_provider.health_check()` / `list_models()`. Запомни структуру.

- [ ] **Step 13.2: Заменить health check на LiteLLM probe**

Логика: `/health/ready` должен проверять, что PostgreSQL отвечает (как было) + LiteLLM жив (`GET <root_url>/health/liveness` → 200).

`<root_url>` = `Config.LLM_BASE_URL` с отрезанным суффиксом `/v1`. Используй однострочник:
```python
root_url = Config.LLM_BASE_URL.removesuffix("/v1").removesuffix("/")
```

Замени блок в `routes/health.py`, который дёргал `engine.llm_provider.health_check()` (или подобное), на:
```python
import httpx

async def _check_litellm() -> bool:
    """Ping LiteLLM /health/liveness. Returns True on HTTP 200."""
    root_url = Config.LLM_BASE_URL.removesuffix("/v1").removesuffix("/")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{root_url}/health/liveness")
            return resp.status_code == 200
    except Exception:
        return False
```

И в endpoint'е `/health/ready` заменить `ollama_ok = ...` на:
```python
    litellm_ok = await _check_litellm()
```

В ответе ключ `ollama` (если был) переименовать в `litellm`:
```python
    return {
        "status": "ready" if (db_ok and litellm_ok) else "degraded",
        "database": "ok" if db_ok else "fail",
        "litellm": "ok" if litellm_ok else "fail",
    }
```

(точная структура ответа зависит от текущего файла — сохрани логику, просто переименуй ключи).

Если в файле был endpoint `/health/models` с вызовом `engine.llm_provider.list_models()` — замени тело на вызов LiteLLM `/v1/models`:
```python
async def list_litellm_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{Config.LLM_BASE_URL}/models",
                headers={"Authorization": f"Bearer {Config.LLM_API_KEY}"},
            )
            if resp.status_code == 200:
                return [m["id"] for m in resp.json().get("data", [])]
    except Exception:
        pass
    return []
```

И endpoint заменяется на вызов `list_litellm_models()`.

- [ ] **Step 13.3: Убрать импорты OllamaProvider**

Если в верхней части файла был `from ..llm.providers.ollama import OllamaProvider` или использование `engine.llm_provider` — удалить.

Добавить в импорты:
```python
from ..config import Config
```
(если ещё не был).

- [ ] **Step 13.4: Проверить нет ли оставшихся ссылок**

Run: `grep -n "OllamaProvider\|llm_provider\|ollama" src/engine/routes/health.py`
Expected: пусто.

- [ ] **Step 13.5: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.routes.health import router; print('ok')"`
Expected: `ok`.

---

## Task 14: Удалить legacy llm/providers/ollama.py

**Files:**
- Delete: `src/engine/llm/providers/ollama.py`
- Delete (conditional): `src/engine/llm/providers/__init__.py`, `src/engine/llm/providers/base.py`, `src/engine/llm/__init__.py`, `src/engine/llm/providers/`, `src/engine/llm/`

- [ ] **Step 14.1: Проверить что никто больше не импортирует OllamaProvider**

Run: `grep -rn "OllamaProvider\|llm.providers.ollama\|llm\.providers" src/engine --include="*.py"`
Expected: пусто. Если что-то осталось — это баг в предыдущих задачах, вернуться и исправить.

- [ ] **Step 14.2: Удалить файл**

Run: `rm src/engine/llm/providers/ollama.py`

- [ ] **Step 14.3: Проверить что ещё лежит в `llm/providers/`**

Run: `ls src/engine/llm/providers/`
Expected: может быть `__init__.py` и `base.py`. Если `base.py` есть — проверь использование:
Run: `grep -rn "BaseLLMProvider\|from.*llm.providers.base" src/engine --include="*.py"`
Expected: пусто (если не-пусто — не удаляй `base.py`, решишь что делать отдельно).

- [ ] **Step 14.4: Удалить папку llm/ если она осиротела**

Если ни `base.py`, ни другие файлы не используются:
Run: `rm -rf src/engine/llm/`

Если что-то ещё используется — оставь только то что нужно, удали только `ollama.py` и при необходимости `providers/__init__.py` если он пустой.

- [ ] **Step 14.5: Импорт-чек — полный**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.app import app; print('ok')"`
Expected: `ok`. Это загружает всю цепочку импортов engine — последний надёжный sanity-check перед деплоем.

---

## Task 15: Деплой на RAG

**Files:** никаких. Только команды на Маке/RAG.

- [ ] **Step 15.1: Обновить `.env` на RAG (ручная правка, не через deploy)**

`.env` не синхронится `deploy.sh`'ом (он в исключениях rsync). На RAG подключись и обнови:

На RAG (через проксмокс-консоль либо по SSH если настроен):
```
nano ~/rugpt/.env
```

Добавить / обновить строки:
```
LLM_BASE_URL=http://192.168.1.80:4000/v1
LLM_API_KEY=sk-dummy
DEFAULT_MODEL=google/gemma-4-31B-it
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
```

Сохранить. Не рестартить движок — рестарт будет в Step 15.4.

- [ ] **Step 15.2: Синхронизировать код на RAG без рестарта**

С Мака (оттуда работает `deploy.sh`):
```
cd ~/rugpt && ./deploy.sh sync
```

Это сделает rsync без pip и без рестарта. Теперь на RAG лежит новый код, но венв старый и движок ещё работает.

- [ ] **Step 15.3: Применить миграцию 020 на RAG**

На RAG (в контейнере rugpt):
```
cd ~/rugpt && ./migrate.sh
```

Expected: увидишь применение миграции 020. Она обновит `role.model_name` во всех активных ролях на `google/gemma-4-31B-it`.

Проверить:
```
PGPASSWORD="$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)" psql -h "$(grep '^DB_HOST=' .env | cut -d= -f2)" -U "$(grep '^DB_USER=' .env | cut -d= -f2)" -d rugpt -c "SELECT code, model_name FROM roles WHERE is_active=true;"
```

Expected: все строки `model_name = google/gemma-4-31B-it`.

- [ ] **Step 15.4: Полный деплой (pip install + рестарт engine)**

С Мака:
```
cd ~/rugpt && ./deploy.sh
```

`deploy.sh` сделает повторный rsync (ничего не поменялось), увидит что `requirements.txt` изменился по сравнению с тем что было, запустит `pip install -r requirements.txt` (поставит `langchain-openai`, удалит `langchain-ollama` если он больше не в requirements — или сам pip оставит его в неиспользуемом виде, это нестрашно), затем `./local_restart.sh engine` перезапустит движок.

Смотри вывод — не должно быть ошибок установки пакетов или ошибок старта.

---

## Task 16: Runtime-проверка

**Files:** нет. Только HTTP-проверки.

- [ ] **Step 16.1: Health check**

С любой машины которая видит RAG через WG:
```
curl -s http://192.168.1.81:8100/api/v1/health/ready
```

Expected: `{"status":"ready","database":"ok","litellm":"ok"}`. Если `litellm:"fail"` — LiteLLM не отвечает / неверный URL → проверь `.env`.

- [ ] **Step 16.2: Прямой тест chat через engine**

```
curl -s -X POST http://192.168.1.81:8100/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"admin@testcompany.ru","password":"test123"}'
```

Expected: получишь `token`. Скопируй его.

Открой webclient в браузере (`https://rugpt.pro`), залогинься `admin@testcompany.ru / test123`, перейди в "Персональный ИИ" → выбери "Размышлятор" → напиши "привет".

Expected: ответ от Gemma-4 через LiteLLM в течение 5-15 сек.

- [ ] **Step 16.3: Тест RAG эмбеддингов**

В webclient перейди в "Файлы" → загрузи любой маленький PDF (на 1 страницу).

Подожди ~10-20 сек. На RAG проверь:
```
PGPASSWORD="$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)" psql -h 192.168.1.82 -U rugpt -d rugpt -c "SELECT original_filename, rag_status, array_length(string_to_array(trim(both '[]' from summary_embedding::text), ','), 1) as dim FROM user_files ORDER BY created_at DESC LIMIT 3;"
```

Expected: `rag_status = 'indexed'`, `dim = 1024`. Если `dim` другое — pgvector схема несовместима с новой моделью (этого быть не должно, Qwen3-Embedding-0.6B = 1024 dim).

- [ ] **Step 16.4: Тест RAG поиска**

В webclient выбери "Поиск по документам" → спроси что-то про загруженный PDF.

Expected: ответ со ссылкой на документ (цитата + название файла).

---

## Task 17: Cleanup (опционально)

**Files:** нет.

- [ ] **Step 17.1: Проверить что langchain-ollama не установлен в венве**

На RAG:
```
./venv/bin/pip list 2>/dev/null | grep -i "langchain-ollama\|langchain_ollama"
```

Expected: пусто (если `deploy.sh` переустановил венв) или показывает версию (если pip оставил старый пакет как неиспользуемый).

Если пакет есть и хочешь убрать:
```
./venv/bin/pip uninstall -y langchain-ollama
```

Не критично. Пакет просто не используется в коде.

---

## Rollback (если что-то пошло не так после Task 15)

1. С Мака: `git revert <commit-hash>` коммита с миграцией. Если коммитили отдельно миграцию + код — revert оба. (Если ещё не коммитили — просто `git checkout -- .` в рабочей копии на Маке.)
2. С Мака: `./deploy.sh sync && ./deploy.sh` — откатит код, pip переустановит старые пакеты, engine перезапустится.
3. На RAG: миграцию 020 откатывать **не нужно**. Старые имена моделей (`qwen3:14b`) снова станут правильными для отката кода (он использует `langchain-ollama` → Ollama напрямую → модель `qwen3:14b` на Ollama).

Если `.env` на RAG уже поменял — верни старые значения:
```
LLM_BASE_URL=http://localhost:11434
DEFAULT_MODEL=qwen3:14b
EMBEDDING_MODEL=qwen3-embedding:0.6b
```
(убери `LLM_API_KEY` строку — она не читалась старым кодом, но лишней не будет).

---

## Self-Review

Прошёлся по spec — что реализовано где:

- **Config изменения** → Tasks 3, 4
- **AgentExecutor** → Task 8
- **Graphs (simple/chain/multi_agent)** → Task 6
- **Rule generator** → Task 7
- **RAGService** → Task 10 (+ Tasks 11, 12 для callers)
- **rag_tool** → Task 9
- **engine_service** → Task 11
- **ingest_queue** → Task 12
- **routes/health.py + удаление OllamaProvider** → Tasks 13, 14
- **Миграция 020** → Task 1, применение в Task 15
- **requirements.txt** → Task 2
- **Runtime-верификация** → Task 16
- **Rollback** → отдельная секция выше

Ничего не пропущено. Имена параметров согласованы: `embedding_model`, `llm_base_url`, `llm_api_key` во всех trёх местах (RAGService, engine_service, ingest_queue).
