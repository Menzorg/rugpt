# LangChain / LangGraph -- выбор и архитектура

## Контекст

Engine ходит к LLM через LiteLLM proxy (OpenAI-compatible) → vLLM на Zver. В коде используется `langchain-openai.ChatOpenAI` / `OpenAIEmbeddings`, направленные на `http://192.168.1.80:4000/v1` с `Bearer sk-dummy`. Детали — `docs/llm.md`.

LangChain/LangGraph нужны потому что прямой HTTP к LLM не масштабируется на:
- Агенты с инструментами (календарь, задачи, поиск по документам)
- RAG (retrieval-augmented generation)
- Управление контекстом (компактизация истории, лимиты на вызовы инструментов)
- Мультиагентные сценарии (супервизор делегирует роли-сабагенту)

## Версии пакетов

Из `requirements.txt` (актуальная экосистема LangChain 1.x):

```
langchain>=1.2.15
langchain-core>=1.2.31
langchain-openai>=1.1.11
langchain-community>=0.4.1
langchain-text-splitters>=1.1.1
langgraph>=1.1.6
langgraph-supervisor>=0.0.31
```

## Экосистема пакетов

```
langchain-core              <- базовые абстракции (LLM, промпты, tools, parsers)
    |
    +-- langgraph           <- графы с состоянием, мультиагентность
    |                          зависит ТОЛЬКО от langchain-core
    |
    +-- langgraph-supervisor <- create_supervisor: оркестрация роли-сабагенты + handoff
    |
    +-- langchain           <- полный пакет (create_agent, middleware, agents)
    |
    +-- langchain-openai    <- ChatOpenAI, OpenAIEmbeddings (работают с LiteLLM)
    |
    +-- langchain-text-splitters <- RecursiveCharacterTextSplitter (RAG-чанкинг)
    |
    +-- langchain-community <- интеграции сообщества (vector stores, tools)
```

Пакеты **не взаимоисключающие** -- используются вместе.

## Что используется в RuGPT

### Оркестрация агентов

**Файлы:** `src/engine/agents/graphs/`

| Граф | Файл | Что делает |
|------|------|------------|
| simple | `simple.py` | Без инструментов — прямой вызов `ChatOpenAI.ainvoke`. С инструментами — ReAct-агент через `langchain.agents.create_agent` (не `create_react_agent`) |
| supervisor | `supervisor.py` | `langgraph_supervisor.create_supervisor`: супервизор + роли-сабагенты, делегирование через handoff-инструменты `transfer_to_<role>` |
| rule_generator | `rule_generator.py` | Одношаговая генерация `rule_text` из обратной связи (вопрос + неверный ответ AI + корректировка пользователя). Прямой `ChatOpenAI.ainvoke` без графа-агента |

В дереве `graphs/` других модулей нет: `chain.py` и `multi_agent.py` удалены — линейные цепочки и ручной `StateGraph` больше не используются.

`AgentExecutor` (`src/engine/agents/executor.py`) — центральный роутер. По `role.agent_type` диспатчит:
- `"simple"` → `run_simple_agent`
- `"supervisor"` → `run_supervisor_agent`
- неизвестное значение → fallback на `run_simple_agent` с warning.

Других значений `agent_type` в коде нет.

### Supervisor: роли-сабагенты и handoff

`run_supervisor_agent` строит граф через `create_supervisor(subagents, model=llm, tools=..., state_schema=SupervisorState, parallel_tool_calls=False)`:

- `_build_subagents` берёт разрешённые сабагент-роли из `role_subagent_service.get_available_subagent_roles(...)`, для каждой собирает отдельный `create_agent` со своим промптом (`is_subagent=True`) и набором инструментов.
- Для каждого сабагента генерируется handoff-инструмент `transfer_to_<agent_name>` (`_create_task_handoff_tool`). Супервизор обязан заполнить `task` и `details` вручную; история диалога сабагенту НЕ передаётся — только явный контекст (`<context>/<task>/<details>`).
- `_SubagentInputWrapper` изолирует вход сабагента (`subagent_messages` в `SupervisorState`), сохраняя историю родителя.
- Супервизорам запрещён параллельный вызов инструментов (`parallel_tool_calls=False`), чтобы не плодить рой агентов разом.

### Вспомогательные модули agents/

Помимо графов, интеграция с LangChain/LangGraph опирается на три модуля рядом с `executor.py`:

| Модуль | Роль |
|--------|------|
| `middleware.py` | `HistoryCompactionMiddleware(AgentMiddleware)` — хук `abefore_model`; когда оценка токенов превышает `trigger_tokens` (23000), суммаризирует все сообщения кроме последних `keep_last` (3) в одну системную сводку, удерживая агента в окне контекста |
| `runtime.py` | `RuntimeContext` — типизированный `@dataclass` (`available_tools_count`, `total_tokens_spent`, `critical_tokens_cap`, per-tool scratch для `list_documents`/`rag_search`, `asyncio.Lock`), передаётся в `create_agent`/`create_supervisor` как `context_schema` и доступен инструментам |
| `metadata.py` | Хелперы `extra_body` для LiteLLM: `resolve_litellm_session_id` (один trace-session на весь прогон через ContextVar), `build_initial_extra_body` (metadata: session_id/agent_name/chat_id/supervisor_name), `append_extra_body_key` (например, `chat_template_kwargs={"enable_thinking": True}`) |

`AgentExecutor` дополнительно навешивает `ToolCallLimitMiddleware` (из `langchain.agents.middleware`): суммарный лимит вызовов инструментов 35 за запрос, отдельный лимит на `rag_search` — 20.

### LLM-провайдер (langchain-openai через LiteLLM)

- `ChatOpenAI(base_url="http://192.168.1.80:4000/v1", api_key="sk-dummy", model=...)` — единственный канал к LLM во всех графах, в `AgentExecutor._create_llm` и в `rule_generator`. Модель по умолчанию: `DEFAULT_MODEL = "google/gemma-4-31B-it"` (без префикса `hosted_vllm/` — маршрутизацию по имени модели делает сам LiteLLM).
- `OpenAIEmbeddings(...)` — эмбеддинги для RAG. `EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"` (1024 dim, `RAG_VECTOR_DIM`).
- LiteLLM proxy на Zver маршрутизирует запросы на vLLM + CUDA.

### RAG-утилиты

- `RecursiveCharacterTextSplitter` (`langchain-text-splitters`) — нарезка документов на чанки (`RAG_CHUNK_SIZE=1000`, `RAG_CHUNK_OVERLAP=200`).
- Вместо document loaders используется Apache Tika для парсинга + pgvector для хранения.

### Инструменты агентов

**Файлы:** `src/engine/agents/tools/` (+ `registry.py`, `util/`)

В `ToolRegistry` (`services/engine_service.py`, метод `initialize()`) регистрируется 19 инструментов. Все реализованы, кроме `role_call` (stub). `web_search` реализован через Perplexity API (модель `sonar`, `PERPLEXITY_API_KEY`). Имена ниже — ровно те строки, под которыми инструменты зарегистрированы (по ним их декларирует `role.tools`).

| Имя в registry | Файл-фабрика | Статус |
|----------------|--------------|--------|
| `calendar_create`, `calendar_query` | `calendar_tool.py` (`create_calendar_tools`) | Работает |
| `task_create`, `task_query`, `task_update`, `task_deadline_proposal`, `get_own_tasks` | `task_tool.py` (`create_task_tools`) | Работает |
| `rag_search` | `rag_tool.py` | Работает (vector + TSV rank fusion) |
| `expand_chunk` | `expand_chunk_tool.py` (`create_expand_chunk_tool`) | Работает |
| `table_rows_search` | `table_rows_tool.py` | Работает |
| `list_documents`, `list_own_documents` | `list_documents.py` | Работает |
| `analyze_image` | `analyze_image.py` (`create_analyze_image_tool`) | Работает |
| `user_search` | `user_tool.py` (`create_user_tools`) | Работает |
| `show_modal` | `show_modal.py` (`create_show_modal_tool`) | Работает |
| `list_invoices`, `get_invoice` | `list_invoices.py`, `get_invoice.py` (фабрики от `engine`) | Работает |
| `web_search` | `web_tool.py` | Работает (Perplexity `sonar`) |
| `role_call` | `role_call_tool.py` | Stub (`Phase 5`) |

> Конкретный набор для роли задаётся в `role.tools`. При несовпадении имени `resolve()` логирует warning и пропускает инструмент.

**Context injection:** org_id, caller_user_id, callee_user_id, invocation_kind, is_admin, timezone, role передаются через `RunnableConfig(configurable={...})` — LLM видит только бизнес-параметры (query, title и т.д.).

### ToolRegistry

**Файл:** `src/engine/agents/tools/registry.py`

Роли декларируют имена инструментов (`role.tools: ["calendar_create", "rag_search", ...]`). `ToolRegistry.resolve(names)` маппит имена на `BaseTool`-инстансы и заодно генерирует строку документации (`{tools}` в промпте), отсутствующие имена логируются как warning.

## LangChain vs LangGraph -- суть разницы

**LangChain** -- библиотека-конструктор. Даёт кирпичики (ChatOpenAI, tools, `create_agent`, middleware) и собирает из них агента. ReAct-агент в `simple` — это уже граф под капотом (`create_agent` возвращает скомпилированный LangGraph-граф).

**LangGraph** -- надстройка для **графов с состоянием**. Узлы и рёбра, общее состояние, циклы, ветвления, делегирование между агентами. Supervisor в RuGPT — именно такой граф: супервизор-узел через handoff передаёт управление узлам-сабагентам и собирает их ответы.

### Аналогия

- **LangChain** = конвейер на заводе. Деталь идёт по ленте: промпт → LLM → инструмент → ответ.
- **LangGraph** = команда работников с общей доской. Супервизор смотрит на доску, решает кому делегировать, передаёт задачу коллеге-сабагенту, собирает результат. Могут работать в цикле.

## Миграция с Ollama завершена

Исторически агенты ходили на локальную Ollama через `ChatOllama` / `OllamaEmbeddings`, а в движке был прямой `OllamaProvider` (HTTP к `:11434`) для health-checks и листинга моделей. Миграция на GPU-хост Zver завершена; стек полностью заменён:

- HTTP-клиент: `langchain-openai` вместо `langchain-ollama`
- Gateway: LiteLLM proxy `:4000` (OpenAI-compatible) вместо Ollama `:11434`
- Inference engine: vLLM + CUDA вместо Ollama CPU
- Модели: `google/gemma-4-31B-it` + `Qwen/Qwen3-Embedding-0.6B` вместо `qwen2.5:7b` + `qwen3-embedding:0.6b`

Legacy-провайдера в коде больше нет: в `src/engine/llm/` и `src/engine/llm/providers/` не осталось `.py`-исходников (только пустые пакеты с `__pycache__`). Весь LLM-доступ идёт через `langchain-openai.ChatOpenAI`/`OpenAIEmbeddings` → LiteLLM → vLLM. Вся агентная работа: `AgentExecutor` → графы (`simple` / `supervisor`) → `ChatOpenAI` → LiteLLM → vLLM.
