# LangChain / LangGraph — выбор и архитектура

## Контекст

Текущая интеграция с LLM — прямой HTTP к Ollama (`OllamaProvider`). Это работает для простого "промпт → ответ", но не масштабируется на:
- Агенты с инструментами (поиск, калькулятор, обращение к БД)
- RAG (retrieval-augmented generation)
- Цепочки обработки (chain of thought, sequential processing)
- Мультиагентные сценарии (один агент вызывает другого)

## Экосистема пакетов

```
langchain-core              ← базовые абстракции (LLM, промпты, tools, parsers)
    │
    ├── langgraph           ← графы с состоянием, мультиагентность
    │                          зависит ТОЛЬКО от langchain-core
    │
    ├── langchain           ← полный пакет (готовые chains, agents, RAG retrieval)
    │
    ├── langchain-community ← интеграции сообщества (vector stores, tools)
    │
    └── langchain-ollama, langchain-openai, ...  ← провайдеры LLM
```

Пакеты **не взаимоисключающие** — можно использовать вместе:

```bash
pip install langgraph langchain langchain-ollama langchain-community
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

- Vector store интеграции (ChromaDB, Milvus, FAISS)
- Дополнительные LLM-провайдеры
- Дополнительные tools (Wikipedia, Google Search и т.д.)

### langgraph

- Графы с состоянием (state machine для агентов)
- Узлы (агенты, инструменты) и рёбра (переходы)
- Циклы, ветвления, контрольные точки
- Общее состояние графа (state), доступное всем узлам
- Мультиагентная оркестрация

## LangChain vs LangGraph — суть разницы

**LangChain** — библиотека-конструктор. Даёт кирпичики и собирает из них **линейную цепочку**: вход → промпт → LLM → парсинг → выход. Цепочка последовательная — каждый шаг знает только про предыдущий.

**LangGraph** — надстройка для **графов с состоянием**. Вместо линейной цепочки строится граф: узлы и рёбра. Агент может зациклиться ("подумай ещё раз"), разветвиться ("нужен поиск — иди в узел поиска, нет — отвечай"), вызвать другого агента и вернуться. У графа есть общее состояние, которое все узлы читают и пишут.

### Аналогия

- **LangChain** = конвейер на заводе. Деталь идёт по ленте: станок 1 → станок 2 → станок 3 → готово.
- **LangGraph** = команда работников с общей доской. Каждый смотрит на доску, решает что делать, пишет результат, передаёт другому. Могут вернуть задачу назад, вызвать коллегу, работать в цикле.

## Что использовать для какой задачи

| Задача | Что нужно |
|--------|-----------|
| Промпт → LLM → ответ | Хватит langchain-core (или текущего HTTP) |
| Промпт → LLM → tool → LLM → ответ | langchain agents |
| RAG (поиск по документам) | langchain + langchain-community (text splitters, vector stores, retrieval chains) |
| Агент думает, вызывает инструменты, проверяет себя, зовёт других агентов | langgraph |
| Мультиагентная система (несколько ролей взаимодействуют) | langgraph |

## Решение для RuGPT

Используем **оба**: LangGraph для оркестрации агентов + LangChain для RAG-утилит и провайдеров.

```bash
pip install langgraph langchain langchain-ollama langchain-community
```

### Почему LangGraph как основа оркестрации

- Роли в RuGPT — потенциально мультиагентные системы, а не просто "промпт → ответ"
- LangGraph позволяет строить графы: роль-юрист может вызвать поиск по документам, проверить себя, вызвать роль-бухгалтера за консультацией
- Общее состояние графа хранит контекст разговора, результаты инструментов, промежуточные решения

### Почему LangChain тоже нужен

- RAG: text splitters, document loaders, retrieval chains — этого нет в LangGraph
- Готовые интеграции с vector stores (ChromaDB/Milvus) через langchain-community
- LangGraph не дублирует эти утилиты, он только добавляет граф и состояние

## Миграция с текущего HTTP

Текущий `OllamaProvider` (прямой HTTP) продолжит работать для простых ролей (`agent_type=simple`). Переход на LangGraph — для сложных ролей с инструментами и мультиагентностью. Миграция постепенная, не требует переписывания всего.
