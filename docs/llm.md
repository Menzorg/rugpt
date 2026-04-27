# RuGPT LLM Integration

Интеграция Engine с LLM через LiteLLM proxy + vLLM на физической GPU-машине Zver (C, `192.168.1.80`). Весь стек — OpenAI-compatible.

> Связь с инфраструктурой: `architecture.md` (узел C, размещение подсистем), `networking.md` (поток B.I.1 → C.1 LiteLLM :4000), `architecture-full-2026-04-22.md` (Zver HW, модели, weak spots).

## Архитектура

```
ChatService · SchedulerService · AgentRequestHandler
    │
    └── AIService
           │
           └── AgentExecutor (LangChain + LangGraph)
                   │
                   ├── simple без tools  → ChatOpenAI.invoke()
                   ├── simple с tools    → LangGraph ReAct agent
                   ├── chain             → последовательные шаги
                   └── multi_agent       → LangGraph StateGraph
                           │
                           └── ChatOpenAI (langchain-openai)
                                   │  base_url = http://192.168.1.80:4000/v1
                                   │  api_key  = sk-dummy
                                   ▼
                           ┌───────────────────────────────────┐
                           │ C. Zver (192.168.1.80)            │
                           │                                   │
                           │  C.1 LiteLLM proxy :4000          │
                           │    Bearer sk-dummy                │
                           │    OpenAI-compatible API          │
                           │       │                           │
                           │       ▼                           │
                           │  C.2 vLLM (Python + CUDA)         │
                           │    • google/gemma-4-31B-it        │
                           │    • Qwen/Qwen3-Embedding-0.6B    │
                           │    ~200 GB VRAM                   │
                           └───────────────────────────────────┘

RAG pipeline:
  RAGService
    ├── OpenAIEmbeddings (langchain-openai) → LiteLLM :4000
    │     model: hosted_vllm/Qwen/Qwen3-Embedding-0.6B (1024-dim)
    └── ChatOpenAI → LiteLLM :4000 (summary generation)
```

**Почему такая цепочка:**
- vLLM быстро инференсит модели на GPU, но управлять маршрутизацией/моделями/алиасами удобнее через LiteLLM.
- LiteLLM — OpenAI-compatible, поэтому на стороне Engine используется стандартный `langchain-openai`, ничего самописного.
- Engine → Zver идёт по LAN `192.168.1.0/24` **в открытом виде** (TLS внутри LAN отсутствует, security-пункт 1). Trust by LAN/VPN isolation.

## AgentExecutor

**Файл:** `src/engine/agents/executor.py`

Центральный компонент для LLM-вызовов. Инкапсулирует все graphs и tools.

```python
class AgentExecutor:
    def __init__(self, base_url, default_model, prompt_cache, tool_registry, timeout=300.0)

    async def execute(role, messages, user_id=None, temperature=0.7, max_tokens=2048) -> AgentResult
```

**Как работает:**

1. `role.agent_type` → выбирает граф (simple / chain / multi_agent)
2. `prompt_cache.get_prompt(role)` → системный промпт из файла (git-версионирование)
3. `tool_registry.resolve(role.tools)` → список LangChain tools
4. `ChatOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, model=role.model_name)` → LLM-клиент
5. `RunnableConfig(configurable={"org_id": ..., "user_id": ...})` → контекст для tools (они читают его через `config.get("configurable")`)
6. Выполняет граф → `AgentResult`

**Графы:**

| Граф | Файл | Описание |
|---|---|---|
| `simple` | `graphs/simple.py` | Прямой вызов LLM либо ReAct agent (если есть tools) |
| `chain` | `graphs/chain.py` | Последовательные шаги из `agent_config["steps"]` |
| `multi_agent` | `graphs/multi_agent.py` | LangGraph `StateGraph` из `agent_config["graph"]` |
| `rule_generator` | `graphs/rule_generator.py` | Генерация правил коррекции из отклонённых AI-ответов |

## Инструменты

**Файл:** `src/engine/agents/tools/`

| Tool | Файл | Описание | Статус |
|---|---|---|---|
| `calendar_create`/`calendar_query` | `calendar_tool.py` | Календарные события | Работает (factory pattern с CalendarService closure) |
| `task_create`/`task_query`/`task_update` | `task_tool.py` | CRUD задач | Работает |
| `rag_search` | `rag_tool.py` | Гибридный поиск по документам (pgvector + TSV) | Работает |
| `web_search` | `web_tool.py` | Веб-поиск | **Stub** |
| `role_call` | `role_call_tool.py` | Вызов другой роли | **Stub** |

`rag_search` использует `RunnableConfig` для получения `org_id` / `user_id` — LLM видит только параметр `query`.

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

Промпты в файлах (не в БД) — git-версионирование + сброс кеша через admin API без рестарта.

```
src/engine/prompts/
├── lawyer.md
├── accountant.md
├── hr.md
├── chu.md
├── admin_assistant.md
├── humorist.md
└── pm.md              # PM-агент (item 10)
```

**Приоритет резолвинга:**
1. `role.prompt_file` → кеш → файл с диска
2. `role.system_prompt` → текст из БД (fallback)

## LiteLLM proxy (C.1 на Zver)

**Где:** `192.168.1.80:4000`, отдельный Python-процесс на Zver. Конфиг — `litellm_config.yaml` (не в репозитории engine).

**Особенности:**
- OpenAI-compatible: `/v1/chat/completions`, `/v1/embeddings`, `/v1/models`
- **Auth**: `Authorization: Bearer sk-dummy` — любой непустой токен. **Slabое место** (security-пункт 5), план ужесточить после Alpha.
- **Health**: `GET /health/liveness` без auth.
- Роль: маршрутизация алиасов модели → vLLM endpoint, логирование, rate-limiting (на уровне LiteLLM, не Engine).

## vLLM (C.2 на Zver)

**Что:** Python + CUDA inference engine, запущен локально на Zver. Загружает модели в VRAM.

**Модели (production):**

| Модель | Роль | Dim | Алиас в LiteLLM |
|---|---|---|---|
| `google/gemma-4-31B-it` | генерация (chat/agent) | — | `hosted_vllm/google/gemma-4-31B-it` |
| `Qwen/Qwen3-Embedding-0.6B` | эмбеддинги (RAG) | **1024** | `hosted_vllm/Qwen/Qwen3-Embedding-0.6B` |

~200 GB VRAM на Zver хватает на одновременную загрузку обеих моделей.

## Проактивный запуск

`SchedulerService` вызывает `AgentExecutor` напрямую для проактивных уведомлений (calendar, morning polls, evening reports):

```python
messages = [{"role": "user", "content": f'Сработало событие: "{event.title}"...'}]
result = await self.agent_executor.execute(role=role, messages=messages)
# result.content -> текст уведомления, далее в NotificationService
```

## Async через Kafka (item 10)

При `@@mention` и auto-respond HTTP-handler не ждёт инференс:

```
POST /chats/{id}/messages
  → AIService.process_ai_mentions:
     1. INSERT agent_runs(status=pending)
     2. publish agent.requests
     3. return {ok, agent_pending=true}   ← HTTP возвращается сразу

[фоновый KafkaConsumerLoop в том же uvicorn]
  → AgentRequestHandler:
     CAS agent_runs: pending → running
     AgentExecutor.execute() → ChatOpenAI → LiteLLM → vLLM
     persist AI message
     agent_runs mark_done
     publish chat.events
  → NestJS KafkaConsumerService
  → SocketGateway.broadcastToChat → WS клиенту
```

Подробности — `architecture.md` раздел «Kafka event bus».

## Конфигурация (.env)

```env
# LLM (LiteLLM на Zver)
LLM_BASE_URL=http://192.168.1.80:4000/v1
LLM_API_KEY=sk-dummy
DEFAULT_MODEL=hosted_vllm/google/gemma-4-31B-it

# RAG Embeddings (тот же LiteLLM endpoint)
EMBEDDING_MODEL=hosted_vllm/Qwen/Qwen3-Embedding-0.6B
RAG_VECTOR_DIM=1024
RAG_CHUNK_SIZE=1000
RAG_CHUNK_OVERLAP=200
RAG_SUMMARY_MODEL=              # по умолчанию = DEFAULT_MODEL
RAG_TIKA_SERVER_ENDPOINT=http://192.168.1.84:9998
RAG_STORE_DSN=                  # по умолчанию = POSTGRES_DSN

# Fallback (опционально)
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini
```

## Legacy: OllamaProvider

**Файл:** `src/engine/llm/providers/ollama.py`

Оставлен для обратной совместимости (health checks + model listing) в коде Engine. В prod-стеке **не используется** — инференс через LiteLLM/vLLM.

```python
class OllamaProvider(BaseLLMProvider):
    async def generate(messages, model?, temperature, max_tokens) -> LLMResponse
    async def health_check() -> bool
    async def list_models() -> List[str]
```

План — убрать после того, как все health/monitoring-зависимости перемигрируют на LiteLLM health endpoint.

## Зависимости (requirements.txt)

```
langchain
langgraph
langchain-openai       # ChatOpenAI + OpenAIEmbeddings (для LiteLLM)
langchain-community
```

`langchain-ollama` больше **не нужен** (только если OllamaProvider всё ещё жив для health).

## Таймауты

- AgentExecutor HTTP timeout — **300 сек** (для CPU/долгого GPU-инференса).
- Nginx на rugpt-container `proxy_read_timeout 300s` для `/api/v1/web/chats/{id}/messages` (когда работает sync-режим, `KAFKA_ENABLED=false`).
- В Kafka-режиме HTTP возвращается мгновенно; реальный лимит инференса — таймаут vLLM (по умолчанию vLLM request timeout).
