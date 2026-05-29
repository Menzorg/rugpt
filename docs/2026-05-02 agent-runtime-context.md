# Runtime Context агентов

Создано: 2026-05-03

Эта заметка описывает runtime-состояние на один запуск, добавленное для simple-агентов на LangGraph, и связанное поведение инструментов.

## Связанная история

На момент написания этой заметки в локальной истории нет коммитов с датой 2026-05-03. Так как работа шла около полуночи, релевантная закоммиченная база находится в истории за 2026-05-02.

Релевантные коммиты 2026-05-02:

- `92e1586` — `23:54 +0400` — `Deduplication of found docs across whole run using ToolRuntime`
- `f202902` — `22:50 +0400` — `fix`
- `17eabd4` — `22:45 +0400` — `output fix`
- `dfa3a91` — `22:39 +0400` — `Reduce max detailed docs limits and change footer`
- `94ed914` — `22:26 +0400` — `Exclude images from list_documents`
- `5f8e78d` — `22:17 +0400` — `less results until description truncates and less char budget`
- `5e6c8ed` — `15:45 +0400` — `attachments dedup and tool doc update`
- `2ed449d` — `15:07 +0400` — `attachments summary or status`
- `ac6a23f` — `14:53 +0400` — `Tie LLM to file attachments`

Рабочее дерево 2026-05-03 расширяет эти коммиты:

- Переносом владения runtime-данными инструментов в `runtime.py`.
- Добавлением `RagSearchRuntimeData`.
- Добавлением адаптивного поведения `rag_search` для `top_k` на основе уже увиденных `chunk_id`.
- Добавлением централизованного управления `RunnableConfig.max_concurrency` в executor.

## Цель

Некоторым инструментам нужна память в рамках одного запуска агента, без раскрытия этого состояния модели и без глобального хранения. Runtime context дает инструментам безопасное место для короткоживущего состояния выполнения: например, уже возвращенных id документов или chunk id.

## RuntimeContext

`src/engine/agents/runtime.py` определяет:

- `RuntimeContext`
- `ListDocumentsRuntimeData`
- `RagSearchRuntimeData`

`RuntimeContext` создается заново внутри `AgentExecutor.execute()` для каждого запуска агента. Он передается simple ReAct агентам как invocation context LangGraph, а не через `RunnableConfig.configurable`.

Важное различие:

- `RunnableConfig.configurable` несет scope вызывающего пользователя, сейчас это `org_id` и `user_id`.
- LangGraph runtime `context` несет изменяемое состояние инструментов на один запуск.

Так состояние инструментов не протекает через config и не переиспользуется между разными запусками.

## Подключение Simple Agent

`run_simple_agent()` принимает опциональный аргумент `context_schema`.

Для simple-агентов с инструментами:

1. Executor создает свежий `RuntimeContext()`.
2. Simple graph передает `type(runtime_context)` в `create_react_agent(..., context_schema=...)`.
3. Simple graph передает сам объект `runtime_context` в `agent.ainvoke(..., context=...)`.
4. Инструменты получают доступ к нему через injected `ToolRuntime[RuntimeContext]`.

Имя параметра `context_schema` сейчас используется для передачи либо типа схемы, либо конкретного экземпляра context. Если передан конкретный экземпляр, graph получает схему через `type(context_schema)` и использует сам экземпляр как invocation context.

## list_documents

`list_documents` использует `runtime.context.list_documents_runtime_data.seen_ids`.

Поведение:

- Каждый возвращенный видимый document id добавляется в `seen_ids`.
- При следующих вызовах `list_documents` в том же запуске агента документы, которые уже есть в `seen_ids`, удаляются из `visible`.
- Если дедупликация произошла, output инструмента получает префикс:

```text
DOCS FOUND IN PREVIOUS TOOL CALLS WERE DEDUPLICATED
```

Header применяется последовательно, включая ветки с пустым результатом.

## rag_search

`rag_search` использует `runtime.context.rag_search_runtime_data.chunk_ids`.

Поведение:

- Возвращенные chunk id добавляются в `chunk_ids`.
- Перед поиском инструмент считает уже увиденные chunk id и корректирует `top_k`.

Текущие пороги:

```text
seen chunk ids <= 15  -> top_k = 4
seen chunk ids > 15   -> top_k = 3
seen chunk ids > 30   -> top_k = 2
```

Это делает повторные поиски менее многословными по мере накопления retrieved context в рамках запуска.

## max_concurrency

`AgentExecutor.execute()` теперь задает `max_concurrency` в `RunnableConfig`.

Это простой центральный контроль параллельного выполнения инструментов. Меньшие значения уменьшают число одновременных tool calls и упрощают рассуждение об обновлениях runtime data. Текущее значение:

```python
RunnableConfig(
    max_concurrency=3,
    configurable={
        "org_id": ...,
        "user_id": ...,
    },
)
```

## Почему Runtime Data живет в runtime.py

Классы tool-specific runtime data лежат рядом с `RuntimeContext`, а не внутри модулей инструментов. Это предотвращает ошибки forward reference в Pydantic, когда LangChain/LangGraph строит схемы инструментов, например:

```text
`list_documents` is not fully defined; you should define `ListDocumentsRuntimeData`, then call `list_documents.model_rebuild()`.
```

Когда классы runtime data находятся в `runtime.py`, Pydantic видит настоящие runtime-visible типы. Это также убирает циклическое владение типами между инструментами и `RuntimeContext`.
