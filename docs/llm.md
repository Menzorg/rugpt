# RuGPT LLM Integration

Интеграция с Large Language Models через агентную систему.

## Архитектура

```
ChatService / SchedulerService
    │
    └── AIService
            │
            └── AgentExecutor (LangChain/LangGraph)
                    │
                    ├── simple (без tools) → ChatOllama.invoke()
                    ├── simple (с tools) → LangGraph ReAct agent
                    ├── chain → Последовательные шаги
                    └── multi_agent → LangGraph StateGraph
                            │
                            └── ChatOllama → Ollama API
                                    │
                                    ├── /api/chat (Ollama native)
                                    └── /v1/chat/completions (OpenAI-compatible)

Legacy (для health checks):
    └── OllamaProvider → HTTP запросы к Ollama
```

## AgentExecutor

**Файл:** `src/engine/agents/executor.py`

Центральный компонент для вызова LLM. Заменил прямые HTTP-вызовы через OllamaProvider.

```python
class AgentExecutor:
    def __init__(self, base_url, default_model, prompt_cache, tool_registry)

    async def execute(role, messages, temperature=0.7, max_tokens=2048) -> AgentResult
```

**Как работает:**
1. Получает `role.agent_type` → выбирает граф
2. `prompt_cache.get_prompt(role)` → системный промпт из файла/кеша
3. `tool_registry.resolve(role.tools)` → список инструментов
4. `ChatOllama(model=role.model_name)` → LLM
5. Выполняет граф → `AgentResult`

**Графы:**
- `simple.py` — прямой вызов или ReAct agent
- `chain.py` — последовательные шаги из `agent_config["steps"]`
- `multi_agent.py` — StateGraph из `agent_config["graph"]`

---

## Инструменты (Tools)

**Файл:** `src/engine/agents/tools/`

Инструменты доступны агентам через `role.tools`:

| Tool | Файл | Описание | Статус |
|------|-------|----------|--------|
| `calendar_create` | calendar_tool.py | Создать событие | Работает |
| `calendar_query` | calendar_tool.py | Запрос событий | Работает |
| `rag_search` | rag_tool.py | Поиск по документам | Stub |
| `web_search` | web_tool.py | Веб-поиск | Stub |
| `role_call` | role_call_tool.py | Вызов другой роли | Stub |

Calendar tools используют factory pattern:
```python
def create_calendar_tools(calendar_service):
    # Возвращает (create_tool, query_tool) с замыканием на CalendarService
```

---

## PromptCache

**Файл:** `src/engine/services/prompt_cache.py`

Промпты хранятся в файлах, не в БД. Git-версионирование.

```
src/engine/prompts/
├── lawyer.md       # Системный промпт юриста
├── accountant.md   # Бухгалтер
├── hr.md           # HR
└── chu.md          # Общий помощник
```

**Приоритет:**
1. `role.prompt_file` → кеш → файл с диска
2. `role.system_prompt` → текст из БД (fallback)

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
# result.content → текст уведомления
```

---

## Конфигурация

**Через .env:**
```env
LLM_BASE_URL=http://localhost:11434
DEFAULT_MODEL=qwen2.5:7b
OPENAI_API_KEY=            # Fallback (optional)
OPENAI_MODEL=gpt-4o-mini
```

## Модели

### Локальные (Ollama/vLLM)
- `qwen2.5:7b` — основная модель
- `qwen2.5:72b` — для сложных задач
- `mistral:7b` — альтернатива
- `llama3:8b` — Meta LLaMA

### Облачные (fallback)
- `gpt-4o-mini` — OpenAI (быстрый)
- `gpt-4o` — OpenAI (мощный)

---

## Будущее развитие

### Kafka для очередей запросов к LLM

```
Engine (FastAPI)                    GPU Server
    │                                   │
    ├── produce: llm.requests ─────────►│ Ollama/vLLM
    │                                   │
    │◄── consume: llm.responses ────────┤
    │                                   │
```

**Зачем:**
- Асинхронная обработка — API не блокируется на ожидании LLM
- Масштабирование через consumer groups — несколько GPU-воркеров
- AI streaming — постепенная отдача ответа пользователю

### RAG (Retrieval-Augmented Generation)
```
Role.rag_collection → Vector DB (ChromaDB/Milvus)
                          │
                          └── Documents (PDF, DOCX, Excel)
```

Tool `rag_search` подготовлен как stub, будет реализован в Фазе 5.
