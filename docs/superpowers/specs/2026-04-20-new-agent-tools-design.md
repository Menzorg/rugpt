# Новые tools для агентов: user_search + list_documents

Дата: 2026-04-20
Статус: design approved

## Мотивация

Из roadmap item 12 — ряд lookup-тулзов для агентов. Берём минимальный первый подход: дать PM возможность искать сотрудников, а `doc_search` — видеть каталог документов организации. Это две самые простые и самые часто востребованные lookup-операции.

## Scope

**Только два новых tool'а. Без чат-ops, без суммаризации, без уведомлений.** Эти три больших блока — отдельные спеки потом.

## Архитектура

Два независимых factory-модуля в `src/engine/agents/tools/`:

- `user_tool.py` → `create_user_tools(user_storage, role_storage, department_service)` → `(user_search_tool,)`
- `document_tool.py` → `create_document_tools(user_file_storage)` → `(list_documents_tool,)`

Оба — async `StructuredTool.from_function(coroutine=...)` (та же схема, что сейчас после фикса в task_tool/calendar_tool).

Контекст инициатора (`user_id`, `org_id`) прокидывается через `RunnableConfig` — LLM их не видит, `EngineService` инжектит при вызове `AgentExecutor`.

## user_search (для PM)

**Аргументы:**
- `name_query: str = ""` — substring match по `User.name` и `User.username`, case-insensitive
- `role_code: str = ""` — фильтр по `Role.code`

**Поведение:**
- Оба пусто → вернуть всех видимых инициатору пользователей
- Только `name_query` → подстрока в name/username
- Только `role_code` → все с этой ролью (код ищется через `role_storage.get_by_code(code, org_id)` затем `list_by_role(role_id)`)
- Оба → пересечение

**Правила видимости:**
- Использует `department_service.get_visible_user_ids(initiator_user_id, org_id)` — ту же модель что task_tools
- Системные пользователи (`is_system=true`) НЕ показываются — они инфраструктура (см. memory feedback_system_users_hidden)
- Ограничение — max 30 записей (больше = LLM захлёбывается)

**Формат вывода:**
```
User search results (N total):
- Ivan Petrov (@ivan_petrov, id=<uuid>, role=lawyer)
- Anna Yurieva (@anna_lawyer, id=<uuid>, role=lawyer)
- ...
```

Если 0 результатов — `"No users match filter."`

## list_documents (для doc_search)

**Аргументы:**
- `name_query: str = ""` — substring match по `UserFile.original_filename`, case-insensitive

**Поведение:**
- Пусто → все видимые документы инициатора в его org
- С `name_query` → фильтр по подстроке

**Правила видимости:**
- Использует модель RAG: `org_id = initiator_org_id AND (uploaded_by_user_id = initiator_user_id OR is_public = true)`
- **НЕ** department-visibility — это domain-specific: каждый видит свои файлы + публичные org, как сейчас в `/files` endpoint'е и RAG поиске
- Только `is_active=true`
- Max 30 записей
- Сортировка по `created_at DESC` (новые сверху)

**Формат вывода:**
```
Documents (N total):
- contract_draft.pdf (id=<uuid>, rag=indexed, summary: "Contract between...")
- report_q3.xlsx (id=<uuid>, rag=indexed, summary: "Quarterly financial...")
- ...
```

`summary` обрезается до ~100 символов + ellipsis. Если `rag_status != 'indexed'` — `summary: —`.

Если 0 результатов — `"No documents in your scope."`

## Registry и привязка к ролям

**`EngineService.initialize()`:**
- Создать `user_search_tool = create_user_tools(...)` и зарегистрировать в `ToolRegistry` под именем `user_search`
- Создать `(list_documents_tool,) = create_document_tools(self.user_file_storage)` и зарегистрировать под именем `list_documents`

**Миграция 021** `021_agent_tools_user_search_list_documents.sql`:
- `pm` role: `tools = tools || '["user_search"]'::jsonb` (append к существующим `["task_create", "task_query", "task_update"]`)
- `doc_search` role: `tools = tools || '["list_documents"]'::jsonb` (append к существующему `["rag_search"]`)
- Идемпотентная через `WHERE NOT (tools @> ...)`:

```sql
UPDATE roles
SET tools = tools || '["user_search"]'::jsonb,
    updated_at = NOW()
WHERE code = 'pm' AND NOT (tools @> '["user_search"]'::jsonb);

UPDATE roles
SET tools = tools || '["list_documents"]'::jsonb,
    updated_at = NOW()
WHERE code = 'doc_search' AND NOT (tools @> '["list_documents"]'::jsonb);
```

## Storage зависимости

Используем уже существующие методы:

**UserStorage:**
- `list_by_org(org_id, active_only=True)` — базовый список
- `list_by_role(role_id)` — для фильтра по role_code

**RoleStorage:**
- `get_by_code(code, org_id)` — перевести `role_code` → `role_id`

**DepartmentService:**
- `get_visible_user_ids(viewer_user_id, org_id) -> Set[UUID]` — источник visibility

**UserFileStorage:**
- Нужен метод `list_visible_for_user(user_id, org_id)` — возвращает is_active=true + (uploaded_by=user_id OR is_public=true) в org
- Если такого нет — добавить. Смотреть текущий код `/files` endpoint'а — там эта логика должна быть, вытащить в storage-метод

Если метод уже есть под другим именем (например `list_by_user_or_public`) — использовать его.

## Не делается в этой спеке

- `chat_post`, `task_chat_post`, `project_chat_post`, `dept_chat_post` — отдельная спека
- `chat_summary`, `project_summary`, `tasks_summary` — отдельная спека (LLM-heavy, дорого, нужен свой дизайн)
- `notify_users`, `notify_department` — отдельная спека
- Расширение `user_search` на департменты/фильтр `is_head` — позже
- Добавление filter `only_indexed` в `list_documents` — YAGNI
- Пагинация — YAGNI (30 записей хватит)

## Тестирование (runtime после деплоя)

- `@@pm найди сотрудников с ролью lawyer` → должен вернуть список юристов org
- `@@pm кто такой Иван?` → поиск по имени
- `@@pm покажи всех сотрудников` → полный список
- В "Поиск по документам": "какие файлы доступны?" → список видимых для юзера файлов
- В "Поиск по документам" залогиниться рядовым юзером → `list_documents` не должен показывать чужие непубличные файлы
