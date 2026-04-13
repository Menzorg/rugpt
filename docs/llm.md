# RuGPT LLM Integration

Интеграция с Large Language Models через агентную систему.

## Архитектура

```
ChatService / SchedulerService
    |
    +-- AIService
            |
            +-- AgentExecutor (LangChain/LangGraph)
                    |
                    +-- simple (без tools) -> ChatOllama.invoke()
                    +-- simple (с tools) -> LangGraph ReAct agent
                    +-- chain -> Последовательные шаги
                    +-- multi_agent -> LangGraph StateGraph
                            |
                            +-- ChatOllama -> Ollama API
                                    |
                                    +-- /api/chat (Ollama native)
                                    +-- /v1/chat/completions (OpenAI-compatible)

RAG pipeline:
    +-- RAGService
            +-- OllamaEmbeddings -> Ollama API (embedding)
            +-- ChatOllama -> summary generation

Legacy (для health checks):
    +-- OllamaProvider -> HTTP запросы к Ollama
```

## AgentExecutor

**Файл:** `src/engine/agents/executor.py`

Центральный компонент для вызова LLM. Заменил прямые HTTP-вызовы через OllamaProvider.

```python
class AgentExecutor:
    def __init__(self, base_url, default_model, prompt_cache, tool_registry, timeout=300.0)

    async def execute(role, messages, user_id=None, temperature=0.7, max_tokens=2048) -> AgentResult
```

**Как работает:**
1. Получает `role.agent_type` -> выбирает граф
2. `prompt_cache.get_prompt(role)` -> системный промпт из файла/кеша
3. `tool_registry.resolve(role.tools)` -> список инструментов
4. `ChatOllama(model=role.model_name)` -> LLM
5. `RunnableConfig(configurable={"org_id": ..., "user_id": ...})` -> контекст для tools
6. Выполняет граф -> `AgentResult`

**Графы:**
- `simple.py` -- прямой вызов или ReAct agent
- `chain.py` -- последовательные шаги из `agent_config["steps"]`
- `multi_agent.py` -- StateGraph из `agent_config["graph"]`
- `rule_generator.py` -- генерация правил коррекции из обратной связи

---

## Инструменты (Tools)

**Файл:** `src/engine/agents/tools/`

Инструменты доступны агентам через `role.tools`:

| Tool | Файл | Описание | Статус |
|------|-------|----------|--------|
| `calendar_create` | calendar_tool.py | Создать событие | Работает |
| `calendar_query` | calendar_tool.py | Запрос событий | Работает |
| `task_create` | task_tool.py | Создать задачу | Работает |
| `task_query` | task_tool.py | Запрос задач | Работает |
| `task_update` | task_tool.py | Обновить задачу | Работает |
| `rag_search` | rag_tool.py | Гибридный поиск по документам | Работает |
| `web_search` | web_tool.py | Веб-поиск | Stub |
| `role_call` | role_call_tool.py | Вызов другой роли | Stub |

Calendar и task tools используют factory pattern:
```python
def create_calendar_tools(calendar_service)  # -> (create_tool, query_tool)
def create_task_tools(task_service)          # -> (create_tool, query_tool, update_tool)
```

`rag_search` использует `RunnableConfig` для получения org_id/user_id -- LLM видит только параметр `query`.

---

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

Промпты хранятся в файлах, не в БД. Git-версионирование.

```
src/engine/prompts/
+-- lawyer.md           # Юрист
+-- accountant.md       # Бухгалтер
+-- hr.md               # HR
+-- chu.md              # Общий помощник
+-- admin_assistant.md  # Административный помощник
+-- humorist.md         # Юморист
```

**Приоритет:**
1. `role.prompt_file` -> кеш -> файл с диска
2. `role.system_prompt` -> текст из БД (fallback)

---

## OllamaProvider (Legacy)

**Файл:** `src/engine/llm/providers/ollama.py`

Оригинальный провайдер, теперь используется только для health checks и model listing.

```python
class OllamaProvider(BaseLLMProvider):
    async def generate(messages, model?, temperature, max_tokens) -> LLMResponse
    async def health_check() -> bool
    async def list_models() -> List[str]
```

---

## Проактивный запуск

SchedulerService вызывает AgentExecutor напрямую (без AIService) для проактивных уведомлений:

```python
# В SchedulerService._build_notification_content():
messages = [{"role": "user", "content": f'Сработало событие: "{event.title}"...'}]
result = await self.agent_executor.execute(role=role, messages=messages)
# result.content -> текст уведомления
```

---

## Конфигурация

**Через .env:**
```env
# LLM
LLM_BASE_URL=http://localhost:11434
DEFAULT_MODEL=qwen2.5:7b
OPENAI_API_KEY=            # Fallback (optional)
OPENAI_MODEL=gpt-4o-mini

# RAG Embeddings
EMBEDDING_MODEL=qwen3-embedding:0.6b
RAG_SUMMARY_MODEL=         # По умолчанию = DEFAULT_MODEL
RAG_TIKA_SERVER_ENDPOINT=http://localhost:9998
RAG_STORE_DSN=             # По умолчанию = POSTGRES_DSN
RAG_VECTOR_DIM=1024
RAG_CHUNK_SIZE=1000
RAG_CHUNK_OVERLAP=200
RAG_SUMMARY_INPUT_MAX_CHARS=8000

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
```

Hardcoded default в коде: `DEFAULT_MODEL=qwen2:0.5b`. Переопределяется через `.env` на `qwen2.5:7b`.

## Модели

### Локальные (Ollama)
- `qwen2.5:7b` -- основная модель (через .env)
- `qwen3-embedding:0.6b` -- модель для эмбеддингов (RAG)

### Облачные (fallback)
- `gpt-4o-mini` -- OpenAI (опционально)
