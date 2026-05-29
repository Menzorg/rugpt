# RuGPT LLM Integration

Интеграция Engine с LLM через **LiteLLM** — OpenAI-совместимый прокси
(`http://192.168.1.80:4000/v1`). Доступ из кода — через
`langchain_openai.ChatOpenAI` (НЕ Ollama, НЕ `ChatOllama`). LiteLLM сам
маршрутизирует запросы на vLLM/прочие бэкенды по имени модели.

Источник истины — код в `src/engine/`. Этот документ описывает ровно то, что
есть в коде.

## Провайдер и клиент

Весь инференс (генерация + эмбеддинги) идёт через один OpenAI-совместимый
gateway. На стороне Engine используется стандартный `langchain-openai`,
самописного провайдера нет.

LLM-клиент собирается в `AgentExecutor._create_llm`
(`src/engine/agents/executor.py`):

```python
from langchain_openai import ChatOpenAI

ChatOpenAI(
    base_url=self.base_url,    # Config.LLM_BASE_URL → http://192.168.1.80:4000/v1
    api_key=self.api_key,      # Config.LLM_API_KEY  → sk-dummy
    model=model,               # role.model_name либо Config.DEFAULT_MODEL
    temperature=temperature,
    timeout=self.timeout,      # 300.0 по умолчанию
    # model_kwargs / extra_body добавляются при необходимости
)
```

`src/engine/llm/` **не содержит ни одного `.py`-файла** — там лежат только
устаревшие `.pyc` (`llm/__pycache__/__init__.cpython-310.pyc` и
`llm/providers/__pycache__/` с `__init__`, `base`, `ollama`). Исходников нет,
класса `OllamaProvider` в коде больше нет; никакого legacy-провайдера Engine
не использует.

## Конфигурация (`src/engine/config.py`)

Дефолты класса `Config` (переопределяются переменными окружения из `.env`):

| Параметр | Env | Дефолт в `config.py` |
|----------|-----|----------------------|
| Base URL | `LLM_BASE_URL` | `http://192.168.1.80:4000/v1` |
| API key | `LLM_API_KEY` | `sk-dummy` |
| Модель чата | `DEFAULT_MODEL` | `google/gemma-4-31B-it` |
| Модель анализа изображений | `IMAGE_ANALYSIS_MODEL` | `= DEFAULT_MODEL` |
| Модель эмбеддингов | `EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` |
| Размерность вектора | `RAG_VECTOR_DIM` | `1024` |
| Модель суммаризации RAG | `RAG_SUMMARY_MODEL` | `= DEFAULT_MODEL` |
| Perplexity key (web_search) | `PERPLEXITY_API_KEY` | `""` (пусто) |

В коде `DEFAULT_MODEL` и `EMBEDDING_MODEL` заданы **без** префикса
`hosted_vllm/` — ровно `google/gemma-4-31B-it` и `Qwen/Qwen3-Embedding-0.6B`.
Префикс провайдера, если нужен, навешивает LiteLLM на своей стороне.

## AgentExecutor

**Файл:** `src/engine/agents/executor.py`

Центральный роутер LLM-вызовов: по `role.agent_type` выбирает граф.

### Конструктор

```python
AgentExecutor(
    base_url: str,
    default_model: str,
    prompt_cache: PromptCache,
    tool_registry: Optional[ToolRegistry] = None,
    timeout: float = 300.0,
    api_key: Optional[str] = None,         # fallback на Config.LLM_API_KEY
    memory_service: Optional[MemoryService] = None,
)
```

Где конструируется — `EngineService.__init__` (`engine_service.py`,
строка ~340): передаются `base_url`, `api_key`, `default_model`,
`prompt_cache`, `tool_registry`. `memory_service` создаётся после
`AgentExecutor` (разрыв циклической зависимости MemoryService ↔ AgentExecutor)
и доинициализируется отдельно. Поле `correction_rule_service` также задаётся
отдельно (по умолчанию `None`).

### execute()

```python
async def execute(
    role: Role,
    messages: List[dict],
    caller_user_id: UUID,
    callee_user_id: Optional[UUID] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    invocation_kind: str = "direct",
    chat_id: Optional[UUID] = None,
) -> AgentResult
```

Идентичность вызова описывается парой `caller_user_id` / `callee_user_id`
(поля `user_id` нет). `invocation_kind` нормализуется внутри: без `chat_id`
становится `"system"`; не `"mention"` или без `callee` — `"direct"`. В
`"mention"` `callee` резолвится из БД, иначе `callee == caller`.

### Как работает (по шагам)

1. `model = role.model_name or self.default_model`.
2. `prompt_cache.get_prompt(role, timezone=...)` → системный промпт.
3. `tool_registry.resolve(role.tools)` → `(tools, tools_doc)`; `{tools}` в
   промпте заменяется на `tools_doc`.
4. `_create_llm(...)` → `ChatOpenAI`. Для не-supervisor включается
   `parallel_tool_calls=True`, для supervisor — `False`.
5. Сборка `RunnableConfig` (см. ниже) + инъекция контекстных блоков (caller/
   callee, org_context, memory summary, correction rules) первым сообщением
   `<context>…</context>`.
6. Middleware: `HistoryCompactionMiddleware` (trigger 23000 токенов, keep_last=3)
   + `ToolCallLimitMiddleware` (общий лимит 35 вызовов; для `rag_search`
   дополнительно 20).
7. Диспетч по `role.agent_type` → граф → `AgentResult`.

### RunnableConfig

`config.configurable` (тулзы читают через `config.get("configurable")`):

| Ключ | Значение |
|------|----------|
| `org_id` | org вызывающего (caller) либо `role.org_id` |
| `caller_user_id` | str(UUID) инициатора |
| `callee_user_id` | str(UUID) адресата (== caller для direct/system) |
| `invocation_kind` | `"direct"` / `"mention"` / `"system"` |
| `is_admin` | bool, по caller |
| `timezone` | таймзона org либо `"Europe/Moscow"` |
| `role` | объект `Role` |

`chat_id` отдельно прокидывается в метаданные LiteLLM-сессии (`extra_body`).

## Маршрутизация agent_type

Распознаются **только два** значения. Любое другое логируется warning'ом и
падает в `simple`:

| agent_type | Граф / функция | Описание |
|------------|----------------|----------|
| `simple` | `run_simple_agent` (`graphs/simple.py`) | Прямой LLM (без tools) либо ReAct-агент (с tools) |
| `supervisor` | `run_supervisor_agent` (`graphs/supervisor.py`) | LangGraph-супервизор + ролевые сабагенты |
| (любое другое) | fallback → `run_simple_agent` | warning в лог |

Значений `chain` и `multi_agent` НЕТ; файлов `chain.py` / `multi_agent.py`
тоже НЕТ. `agent_config["steps"]` и `agent_config["graph"]` нигде не читаются.

## Графы

**Каталог:** `src/engine/agents/graphs/`. Реально существуют ровно три файла:
`simple.py`, `supervisor.py`, `rule_generator.py`.

### simple (`graphs/simple.py`)

`run_simple_agent`:
- **без tools** → `_direct_llm_call`: прямой `llm.ainvoke(messages)`,
  `agent_type="simple"`.
- **с tools** → `_react_agent_call`: агент строится через
  `langchain.agents.create_agent` (НЕ `create_react_agent`), с
  `system_prompt`, `middleware`, `context_schema=RuntimeContext`. Результат —
  `agent_type="simple+tools"`. Перед вызовом LLM биндится
  `chat_template_kwargs={"enable_thinking": True}`.

### supervisor (`graphs/supervisor.py`)

`run_supervisor_agent` → `_supervisor_agent_call`:
- Использует `langgraph_supervisor.create_supervisor`.
- `supervisor_name = agent_config.get("supervisor_name", "supervisor")`.
- Сабагенты строятся в `_build_subagents`: список ролей берётся из
  `engine.role_subagent_service.get_available_subagent_roles(supervisor_role.id)`
  (НЕ из `agent_config`). Для каждой роли — свой `create_agent` с её промптом,
  тулзами и именем `_agent_name(role.code)`.
- Делегирование — через handoff-тулзы `transfer_to_{agent_name}`
  (`_create_task_handoff_tool`): передают только `task` + `details` +
  общий `subagent_context`, история чата сабагенту не уходит.
- `create_supervisor(..., parallel_tool_calls=False)`; граф компилируется
  `workflow.compile(name=supervisor_name)`. Результат — `agent_type="supervisor"`.

### rule_generator (`graphs/rule_generator.py`)

`generate_rule_text(base_url, model, user_question, ai_answer, correction_text,
temperature=0.3)` — НЕ граф агента, а standalone-хелпер. Создаёт свой
`ChatOpenAI` и напрямую `ainvoke` с системным промптом-формулировщиком правил.
В маршрутизацию `agent_type` не входит; вызывается из системы correction rules.

## Инструменты

**Каталог:** `src/engine/agents/tools/`. `ToolRegistry`
(`tools/registry.py`) — реестр имя → `BaseTool`.

`resolve(tool_names)` возвращает **кортеж** `(List[BaseTool], str)`: список
инструментов и склеенную doc-строку (markdown из
`prompts/tools/{name}.md` через разделитель `\n---\n`, для инъекции в `{tools}`
системного промпта). Неизвестные имена пропускаются с warning'ом.

Регистрация — в `EngineService` (`engine_service.py`): основной блок (строки
315–336) и доинициализация после `register_all_actions` (`show_modal`, строки
530–532) и инвойсов (строки 536–539). Всего **19 инструментов**:

| Инструмент | Файл | Фабрика / источник |
|------------|------|--------------------|
| `calendar_create` | `calendar_tool.py` | `create_calendar_tools(calendar_service)` |
| `calendar_query` | `calendar_tool.py` | `create_calendar_tools(calendar_service)` |
| `task_create` | `task_tool.py` | `create_task_tools(task_service)` |
| `task_query` | `task_tool.py` | `create_task_tools(task_service)` |
| `task_update` | `task_tool.py` | `create_task_tools(task_service)` |
| `task_deadline_proposal` | `task_tool.py` | `create_task_tools(task_service)` |
| `get_own_tasks` | `task_tool.py` | `create_task_tools(task_service)` |
| `rag_search` | `rag_tool.py` | `rag_search` |
| `expand_chunk` | `expand_chunk_tool.py` | `create_expand_chunk_tool(...)` |
| `table_rows_search` | `table_rows_tool.py` | `table_rows_search` |
| `web_search` | `web_tool.py` | `web_search` |
| `role_call` | `role_call_tool.py` | `role_call` (**заглушка**) |
| `list_documents` | `list_documents.py` | `list_documents` |
| `list_own_documents` | `list_documents.py` | `list_own_documents` |
| `analyze_image` | `analyze_image.py` | `create_analyze_image_tool(...)` |
| `user_search` | `user_tool.py` | `create_user_tools(...)` |
| `show_modal` | `show_modal.py` | `create_show_modal_tool(action_registry)` |
| `list_invoices` | `list_invoices.py` | `create_list_invoices_tool(self)` |
| `get_invoice` | `get_invoice.py` | `create_get_invoice_tool(self)` |

`create_task_tools` возвращает **5** инструментов одним кортежем
(`task_create`, `task_query`, `task_update`, `task_deadline_proposal`,
`get_own_tasks`).

### Особые случаи

- **`role_call` — единственная заглушка.** Не делегирует реально: логирует
  вызов и возвращает строку
  `"Delegated to {role_code}. (Cross-role calls will be active in Phase 5)"`.
  Кросс-ролевые вызовы в проде идут через supervisor-граф, не через этот тул.
- **`web_search` реализован через Perplexity** (`web_tool.py`, async, модель
  `sonar`, endpoint `https://api.perplexity.ai/chat/completions`). Без
  `PERPLEXITY_API_KEY` мягко возвращает сообщение «Веб-поиск временно
  недоступен… не настроен ключ Perplexity API», ошибку не кидает. При успехе
  добавляет блок «Источники:» с пронумерованными URL.
- Тулзы вроде `rag_search` берут `org_id` / идентичность из `RunnableConfig`,
  LLM видит только содержательные параметры (например `query`).

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

Промпты — в файлах `src/engine/prompts/*.md` (git-версионирование, сброс кеша
через admin API без рестарта), не в БД. Резолвинг — по `role.prompt_file`
с fallback на `role.system_prompt` из БД. Поддерживается режим сабагента
(`is_subagent=True`).

## Таймауты

- `AgentExecutor` HTTP timeout — **300 сек** (дефолт конструктора), для долгого
  GPU-инференса.
- Лимиты вызовов инструментов: 35 суммарно, 20 для `rag_search`
  (`ToolCallLimitMiddleware`).
- История режется `HistoryCompactionMiddleware` при превышении 23000 токенов.

## Конфигурация (.env)

```env
# LLM (LiteLLM-прокси)
LLM_BASE_URL=http://192.168.1.80:4000/v1
LLM_API_KEY=sk-dummy
DEFAULT_MODEL=google/gemma-4-31B-it

# RAG / эмбеддинги (тот же endpoint)
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_VECTOR_DIM=1024
RAG_SUMMARY_MODEL=              # по умолчанию = DEFAULT_MODEL

# web_search (Perplexity), опционально
PERPLEXITY_API_KEY=
```

> Примечание: в текущем рабочем `.env` имена моделей записаны с префиксом
> `hosted_vllm/` (`hosted_vllm/google/gemma-4-31B-it`,
> `hosted_vllm/Qwen/Qwen3-Embedding-0.6B`) — это деталь конфигурации LiteLLM на
> конкретной машине. Дефолты в `config.py` префикса не содержат.

## Зависимости

```
langchain               # create_agent
langgraph               # графы, middleware, prebuilt
langgraph-supervisor    # create_supervisor (supervisor-граф)
langchain-openai        # ChatOpenAI → LiteLLM
langchain-core          # BaseTool, RunnableConfig, messages
httpx                   # web_search (Perplexity)
```

`langchain-ollama` не используется — провайдер Ollama из кода удалён.
