# LangChain / LangGraph -- выбор и архитектура

## Контекст

Engine ходит к LLM через LiteLLM proxy (OpenAI-compatible) → vLLM на Zver. В коде используется `langchain-openai.ChatOpenAI` / `OpenAIEmbeddings`, направленные на `http://192.168.1.80:4000/v1` с `Bearer sk-dummy`. Детали — `docs/llm.md`.

LangChain/LangGraph нужны потому что прямой HTTP к LLM не масштабируется на:
- Агенты с инструментами (календарь, задачи, поиск по документам)
- RAG (retrieval-augmented generation)
- Цепочки обработки (chain of thought, sequential processing)
- Мультиагентные сценарии (один агент вызывает другого)

## Экосистема пакетов

```
langchain-core              <- базовые абстракции (LLM, промпты, tools, parsers)
    |
    +-- langgraph           <- графы с состоянием, мультиагентность
    |                          зависит ТОЛЬКО от langchain-core
    |
    +-- langchain           <- полный пакет (готовые chains, agents, RAG retrieval)
    |
    +-- langchain-community <- интеграции сообщества (vector stores, tools)
    |
    +-- langchain-openai    <- ChatOpenAI, OpenAIEmbeddings (работают с LiteLLM)
```

Пакеты **не взаимоисключающие** -- используем вместе:

```bash
pip install langgraph langchain langchain-openai langchain-community
```

## Что даёт каждый пакет

### langchain-core (ставится автоматически с langgraph)

- Подключение к LLM (базовые интерфейсы)
- Шаблоны промптов (`ChatPromptTemplate`)
- Tools / function calling
- Output parsers (JSON, structured)
- Streaming
- Runnables (композиция вызовов)

### langchain (полный пакет)

- Готовые chains (`LLMChain`, `SequentialChain`, `MapReduceChain`)
- Готовые agents (`ReAct`, `OpenAI Functions Agent`)
- Retrieval chains для RAG (`stuff`, `map_reduce`, `refine`)
- Text splitters (разбиение документов на чанки для RAG)
- Document loaders (PDF, Word, HTML, CSV)

### langchain-community

- Vector store интеграции (pgvector, ChromaDB, FAISS и др.)
- Дополнительные LLM-провайдеры
- Дополнительные tools

### langgraph

- Графы с состоянием (state machine для агентов)
- Узлы (агенты, инструменты) и рёбра (переходы)
- Циклы, ветвления, контрольные точки
- Общее состояние графа (state), доступное всем узлам
- Мультиагентная оркестрация

## Что используется в RuGPT

### Оркестрация агентов (LangGraph)

**Файлы:** `src/engine/agents/graphs/`

| Граф | Описание |
|------|----------|
| `simple.py` | Прямой вызов ChatOpenAI или ReAct agent (через `create_react_agent`) |
| `chain.py` | Последовательные шаги из `agent_config["steps"]` |
| `multi_agent.py` | LangGraph StateGraph с узлами и рёбрами |
| `rule_generator.py` | Генерация правил коррекции AI из обратной связи |

### LLM-провайдер (langchain-openai через LiteLLM)

- `ChatOpenAI(base_url="http://192.168.1.80:4000/v1", api_key="sk-dummy")` — основной LLM-вызов во всех графах и `AgentExecutor`. Модель: `hosted_vllm/google/gemma-4-31B-it`.
- `OpenAIEmbeddings(...)` — эмбеддинги для RAG. Модель: `hosted_vllm/Qwen/Qwen3-Embedding-0.6B` (1024 dim).
- LiteLLM маршрутизирует на vLLM на Zver. OllamaProvider оставлен как legacy для health/listing.

### RAG-утилиты (langchain)

- `RecursiveCharacterTextSplitter` -- нарезка документов на чанки (1000 символов, overlap 200)
- Вместо S3/ChromaDB document loaders используется Apache Tika для парсинга + pgvector для хранения

### Инструменты агентов

**Файлы:** `src/engine/agents/tools/`

| Tool | Описание | Статус |
|------|----------|--------|
| `calendar_create` | Создание событий (factory: CalendarService) | Работает |
| `calendar_query` | Запрос событий | Работает |
| `task_create` | Создание задач (factory: TaskService) | Работает |
| `task_query` | Запрос задач | Работает |
| `task_update` | Обновление задач | Работает |
| `rag_search` | Гибридный поиск: vector + TSV rank fusion | Работает |
| `web_search` | Веб-поиск | Stub |
| `role_call` | Вызов другой роли | Stub |

**Context injection:** org_id и user_id передаются через `RunnableConfig(configurable={...})` -- LLM видит только бизнес-параметры (query, title, и т.д.).

### ToolRegistry

**Файл:** `src/engine/agents/tools/registry.py`

Роли декларируют имена инструментов (`role.tools: ["calendar_create", "rag_search"]`). `ToolRegistry.resolve()` маппит имена на `BaseTool` инстансы.

## LangChain vs LangGraph -- суть разницы

**LangChain** -- библиотека-конструктор. Даёт кирпичики и собирает из них **линейную цепочку**: вход -> промпт -> LLM -> парсинг -> выход. Цепочка последовательная -- каждый шаг знает только про предыдущий.

**LangGraph** -- надстройка для **графов с состоянием**. Вместо линейной цепочки строится граф: узлы и рёбра. Агент может зациклиться ("подумай ещё раз"), разветвиться ("нужен поиск -- иди в узел поиска, нет -- отвечай"), вызвать другого агента и вернуться. У графа есть общее состояние, которое все узлы читают и пишут.

### Аналогия

- **LangChain** = конвейер на заводе. Деталь идёт по ленте: станок 1 -> станок 2 -> станок 3 -> готово.
- **LangGraph** = команда работников с общей доской. Каждый смотрит на доску, решает что делать, пишет результат, передаёт другому. Могут вернуть задачу назад, вызвать коллегу, работать в цикле.

## Миграция с Ollama на LiteLLM

Исторически агенты ходили на локальную Ollama через `ChatOllama` / `OllamaEmbeddings`. После миграции на GPU-хост Zver (C) стек заменён:

- HTTP-клиент: `langchain-openai` вместо `langchain-ollama`
- Gateway: LiteLLM proxy `:4000` (OpenAI-compatible) вместо Ollama `:11434`
- Inference engine: vLLM + CUDA вместо Ollama CPU
- Модели: `google/gemma-4-31B-it` + `Qwen/Qwen3-Embedding-0.6B` вместо `qwen2.5:7b` + `qwen3-embedding:0.6b`

`OllamaProvider` (прямой HTTP) оставлен как legacy для health checks и model listing в коде engine. Агентная работа — через LangChain/LangGraph (`AgentExecutor` → графы → `ChatOpenAI` → LiteLLM → vLLM).
