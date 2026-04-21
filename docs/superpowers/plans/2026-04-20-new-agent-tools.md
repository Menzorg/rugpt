# New Agent Tools — `user_search` + `list_documents` Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement sequentially.

**Goal:** Добавить два новых async-tool'а: `user_search` (на PM — поиск сотрудников по имени/роли с учётом visibility) и `list_documents` (на doc_search — список видимых файлов в org).

**Architecture:** Два новых файла в `src/engine/agents/tools/` через factory-pattern (как task_tool/calendar_tool). Оба tool'а async (`StructuredTool.from_function(coroutine=...)`). Регистрация в `engine_service.py`. Привязка к ролям через миграцию 021 (append в `role.tools` JSONB).

**Tech Stack:** Python 3.10+, LangChain, `langchain_openai` для ChatOpenAI, asyncpg (через уже существующие storage и services).

**Спецификация:** `docs/superpowers/specs/2026-04-20-new-agent-tools-design.md`

**Git:** Пользователь коммитит сам. Шагов `git commit` в плане нет.

---

## File Structure

**Создаются:**
- `src/engine/agents/tools/user_tool.py` — factory `create_user_tools(user_storage, role_storage, department_service)` → `(user_search_tool,)`
- `src/engine/agents/tools/document_tool.py` — factory `create_document_tools(user_file_storage)` → `(list_documents_tool,)`
- `src/engine/migrations/021_agent_tools_user_search_list_documents.sql` — append tool names в `role.tools`

**Редактируются:**
- `src/engine/services/engine_service.py` — создание tools + регистрация в `ToolRegistry`

**Существующие методы storage которые используем (не меняем):**
- `UserStorage.list_by_org(org_id, active_only=True)` — все пользователи org
- `UserStorage.list_by_role(role_id)` — пользователи с ролью (не фильтрует по org, но роль уже scoped по org)
- `RoleStorage.get_by_code(code, org_id)` — найти role по коду
- `DepartmentService.get_visible_user_ids(viewer_user_id, org_id) -> Set[UUID]` — visibility
- `UserFileStorage.list_by_org(org_id)` — уже фильтрует `is_active=true` и сортирует `created_at DESC`

---

## Task 1: document_tool.py

**Files:**
- Create: `src/engine/agents/tools/document_tool.py`

- [ ] **Step 1.1: Создать файл**

Запиши в `src/engine/agents/tools/document_tool.py`:

```python
"""
Document Tools

LangChain tool for listing documents available to an agent's initiator.

Visibility model mirrors RAG / /files endpoint: caller sees their own files
plus `is_public` files within the same org. No department visibility here —
file ownership is the access gate.

Async `StructuredTool.from_function(coroutine=...)` — invoked directly in
the running event loop alongside asyncpg pool.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

logger = logging.getLogger("rugpt.agents.tools.document")

_MAX_RESULTS = 30
_SUMMARY_MAX_CHARS = 100


class ListDocumentsInput(BaseModel):
    name_query: str = Field(
        default="",
        description="Optional substring filter on original filename (case-insensitive). Empty = all visible documents.",
    )


def create_document_tools(user_file_storage):
    """Create document tools wired to a UserFileStorage instance.

    Returns (list_documents_tool,).
    """

    async def _list_documents_async(
        name_query: str = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """List documents available to the caller.

        Args:
            name_query: Substring filter on filename, case-insensitive. Empty = all.
        """
        logger.info(f"tool list_documents: name_query={name_query!r}")
        try:
            configurable = (config or {}).get("configurable", {})
            user_id_str = configurable.get("user_id", "")
            org_id_str = configurable.get("org_id", "")
            if not user_id_str or not org_id_str:
                return "list_documents unavailable: missing context."

            user_id = UUID(user_id_str)
            org_id = UUID(org_id_str)

            all_files = await user_file_storage.list_by_org(org_id)
            # Visibility: uploaded by caller OR is_public
            visible = [
                f for f in all_files
                if f.uploaded_by_user_id == user_id or f.is_public
            ]

            if name_query:
                q = name_query.lower()
                visible = [
                    f for f in visible
                    if q in (f.original_filename or "").lower()
                ]

            if not visible:
                return "No documents in your scope."

            total = len(visible)
            visible = visible[:_MAX_RESULTS]

            lines = []
            for f in visible:
                if f.rag_status == "indexed" and f.summary:
                    summary = f.summary[:_SUMMARY_MAX_CHARS]
                    if len(f.summary) > _SUMMARY_MAX_CHARS:
                        summary += "..."
                    summary_part = f'summary: "{summary}"'
                else:
                    summary_part = "summary: —"
                lines.append(
                    f"- {f.original_filename} (id={f.id}, rag={f.rag_status}, {summary_part})"
                )

            more = f" (showing first {_MAX_RESULTS})" if total > _MAX_RESULTS else ""
            return f"Documents ({total} total{more}):\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"list_documents failed: {e}")
            return f"Failed to list documents: {e}"

    list_tool = StructuredTool.from_function(
        coroutine=_list_documents_async,
        name="list_documents",
        description=(
            "List documents visible to the caller in their organization. "
            "Use when the user asks what files are available, to browse the catalog, "
            "or before calling rag_search to check if the needed document exists."
        ),
        args_schema=ListDocumentsInput,
    )

    return (list_tool,)
```

- [ ] **Step 1.2: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.tools.document_tool import create_document_tools; print('ok')"`
Expected: `ok`

---

## Task 2: user_tool.py

**Files:**
- Create: `src/engine/agents/tools/user_tool.py`

- [ ] **Step 2.1: Создать файл**

Запиши в `src/engine/agents/tools/user_tool.py`:

```python
"""
User Tools

LangChain tool for searching/listing users visible to the caller.

Visibility uses DepartmentService.get_visible_user_ids — same model the task
tools use. System users (is_system=true) are filtered out — they are
infrastructure, not people, and must never leak into any user-list surface.

Async `StructuredTool.from_function(coroutine=...)` — invoked directly in
the running event loop alongside asyncpg pool.
"""
import logging
from typing import Annotated, Optional
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, InjectedToolArg
from pydantic import BaseModel, Field

logger = logging.getLogger("rugpt.agents.tools.user")

_MAX_RESULTS = 30


class UserSearchInput(BaseModel):
    name_query: str = Field(
        default="",
        description="Substring filter on user's name or username (case-insensitive). Empty = no filter.",
    )
    role_code: str = Field(
        default="",
        description="Filter users by role code (e.g. 'lawyer', 'accountant'). Empty = no filter.",
    )


def create_user_tools(user_storage, role_storage, department_service):
    """Create user tools. Returns (user_search_tool,)."""

    async def _user_search_async(
        name_query: str = "",
        role_code: str = "",
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        """Search/list users visible to the caller.

        Args:
            name_query: Substring filter on name or username, case-insensitive.
            role_code: Filter by role code.
        """
        logger.info(
            f"tool user_search: name_query={name_query!r} role_code={role_code!r}"
        )
        try:
            configurable = (config or {}).get("configurable", {})
            user_id_str = configurable.get("user_id", "")
            org_id_str = configurable.get("org_id", "")
            if not user_id_str or not org_id_str:
                return "user_search unavailable: missing context."

            viewer_id = UUID(user_id_str)
            org_id = UUID(org_id_str)

            # Filter by role_code if given — use list_by_role, then intersect with org.
            if role_code:
                role = await role_storage.get_by_code(role_code.strip(), org_id)
                if role is None:
                    return f"No role with code '{role_code}' in your organization."
                candidates = await user_storage.list_by_role(role.id)
                # list_by_role does not filter by org_id — keep only caller's org
                # and only active/non-system.
                candidates = [
                    u for u in candidates
                    if u.org_id == org_id and u.is_active and not u.is_system
                ]
            else:
                candidates = await user_storage.list_by_org(org_id, active_only=True)
                candidates = [u for u in candidates if not u.is_system]

            # Visibility filter (departments model).
            visible_ids = await department_service.get_visible_user_ids(viewer_id, org_id)
            candidates = [u for u in candidates if u.id in visible_ids]

            # Name substring filter.
            if name_query:
                q = name_query.lower()
                candidates = [
                    u for u in candidates
                    if q in (u.name or "").lower() or q in (u.username or "").lower()
                ]

            if not candidates:
                return "No users match filter."

            # Resolve role codes for output — bulk to avoid N+1.
            role_ids = {u.role_id for u in candidates if u.role_id is not None}
            role_code_by_id = {}
            for rid in role_ids:
                r = await role_storage.get_by_id(rid)
                if r is not None:
                    role_code_by_id[rid] = r.code

            total = len(candidates)
            candidates = candidates[:_MAX_RESULTS]

            lines = []
            for u in candidates:
                rc = role_code_by_id.get(u.role_id, "—") if u.role_id else "—"
                username = f"@{u.username}" if u.username else "(no username)"
                lines.append(
                    f"- {u.name} ({username}, id={u.id}, role={rc})"
                )

            more = f" (showing first {_MAX_RESULTS})" if total > _MAX_RESULTS else ""
            return f"User search results ({total} total{more}):\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"user_search failed: {e}")
            return f"Failed to search users: {e}"

    search_tool = StructuredTool.from_function(
        coroutine=_user_search_async,
        name="user_search",
        description=(
            "Search or list users in the caller's organization who are visible to them "
            "(department-based visibility). Supports optional substring name filter and "
            "role code filter. Use to find an employee's UUID before task_create, or "
            "to answer 'who does X?' questions."
        ),
        args_schema=UserSearchInput,
    )

    return (search_tool,)
```

- [ ] **Step 2.2: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.agents.tools.user_tool import create_user_tools; print('ok')"`
Expected: `ok`

---

## Task 3: engine_service.py — регистрация tools

**Files:**
- Modify: `src/engine/services/engine_service.py`

- [ ] **Step 3.1: Добавить импорты**

В верхней части `engine_service.py` найти блок с импортами других tools (там должно быть `from ..agents.tools.task_tool import create_task_tools` или подобные). Добавить рядом:

```python
from ..agents.tools.user_tool import create_user_tools
from ..agents.tools.document_tool import create_document_tools
```

Проверить: `grep -n "from ..agents.tools" src/engine/services/engine_service.py`. Должно показать все импорты tools в одном месте.

- [ ] **Step 3.2: Создать tools и зарегистрировать**

Найти в `engine_service.py` блок вокруг строки 227-235 где создаётся `ToolRegistry` и регистрируются существующие tools (`calendar_create`, `task_create` и т.д.). Перед строкой `self.tool_registry.register("role_call", role_call)` добавить:

```python
        (user_search_tool,) = create_user_tools(
            user_storage=self.user_storage,
            role_storage=self.role_storage,
            department_service=self.department_service,
        )
        self.tool_registry.register("user_search", user_search_tool)

        (list_documents_tool,) = create_document_tools(
            user_file_storage=self.user_file_storage,
        )
        self.tool_registry.register("list_documents", list_documents_tool)
```

Критично: порядок — создаём tools **после** того как `self.department_service` уже проинициализирован (это должно быть раньше в `initialize()`), и `self.user_file_storage` тоже существует. Проверить по коду engine_service.py что оба доступны к моменту создания ToolRegistry.

- [ ] **Step 3.3: Проверить что `department_service` и `user_file_storage` инициализированы раньше ToolRegistry**

Run: `grep -n "self.department_service\s*=\|self.user_file_storage\s*=\|self.tool_registry\s*=" src/engine/services/engine_service.py`

Expected: `self.department_service = ...` и `self.user_file_storage = ...` на строках **меньше** чем `self.tool_registry = ToolRegistry()`. Если нет — перенести соответствующие инициализации выше (но скорее всего уже ок — task_tools тоже их используют, значит инициализация правильная).

- [ ] **Step 3.4: Импорт-чек**

Run: `cd /root/rugpt && ./venv/bin/python -c "from src.engine.app import app; print('ok')"`
Expected: `ok`.

---

## Task 4: Миграция 021

**Files:**
- Create: `src/engine/migrations/021_agent_tools_user_search_list_documents.sql`

- [ ] **Step 4.1: Создать файл миграции**

Запиши в `src/engine/migrations/021_agent_tools_user_search_list_documents.sql`:

```sql
-- Migration 021: Attach new tools to PM and doc_search roles.
-- Idempotent via JSONB containment check (@>).

UPDATE roles
SET tools = tools || '["user_search"]'::jsonb,
    updated_at = NOW()
WHERE code = 'pm' AND NOT (tools @> '["user_search"]'::jsonb);

UPDATE roles
SET tools = tools || '["list_documents"]'::jsonb,
    updated_at = NOW()
WHERE code = 'doc_search' AND NOT (tools @> '["list_documents"]'::jsonb);
```

- [ ] **Step 4.2: Проверить синтаксис**

Run: `cat src/engine/migrations/021_agent_tools_user_search_list_documents.sql`
Expected: файл читается, содержит два UPDATE.

---

## Task 5: Деплой и верификация

**Files:** нет. Команды на Маке/RAG.

- [ ] **Step 5.1: Синк на RAG**

С Мака:
```
cd ~/rugpt && ./deploy.sh sync
```

- [ ] **Step 5.2: Применить миграцию 021 на RAG**

На RAG:
```
cd ~/rugpt && ./migrate.sh
```

Ожидаем строку `Running migration: 021_agent_tools_user_search_list_documents.sql` и успешный finish.

Проверить что tools прописались:
```
set -a; source .env; set +a; PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT code, tools FROM roles WHERE code IN ('pm', 'doc_search');"
```
Expected: у `pm` в tools есть `user_search`, у `doc_search` — `list_documents`.

- [ ] **Step 5.3: Полный деплой (rsync кода + рестарт)**

С Мака:
```
cd ~/rugpt && ./deploy.sh
```

- [ ] **Step 5.4: Health check**

```
curl -s http://192.168.1.81:8100/api/v1/health/ready
```
Expected: `{"ready":true,"litellm":"ok",...}`

- [ ] **Step 5.5: Runtime тест в браузере — user_search**

Залогиниться в webclient admin'ом → открыть direct-чат с PM (через "Персональный ИИ" → PM-агент). Написать по-русски:

1. "Покажи всех сотрудников" — PM должен вызвать `user_search()` без параметров → вернуть список всех видимых.
2. "Найди пользователей с ролью lawyer" — PM вызовет `user_search(role_code="lawyer")` → список юристов.
3. "Найди Анну" — PM вызовет `user_search(name_query="Анна")` → Анна Юрьевна.

Смотреть engine-лог через screen dump чтобы видеть реальные tool-invocations:
```
screen -S rugpt-engine -X hardcopy -h /tmp/engine.dump && grep -E "tool user_search|rugpt.agents.executor" /tmp/engine.dump | tail -20
```

- [ ] **Step 5.6: Runtime тест в браузере — list_documents**

В webclient: "Персональный ИИ" → "Поиск по документам". Предварительно убедиться что в org есть хотя бы один загруженный файл. Написать:

1. "Какие документы доступны?" — агент вызовет `list_documents()` → вернёт список видимых файлов.

Лог:
```
screen -S rugpt-engine -X hardcopy -h /tmp/engine.dump && grep -E "tool list_documents" /tmp/engine.dump | tail -10
```

- [ ] **Step 5.7: Visibility-проверка (опционально)**

Залогиниться как `anna@testcompany.ru` / `test123` (не-admin). В чате с PM: "Покажи всех сотрудников". Результат должен включать только пользователей видимых Анне — admin и она видны всем без отделов (см. `get_visible_user_ids`: users с `department_id=None` видны всем; Иван-admin виден всем).

Цель: убедиться что visibility не-admin'у работает — пустой отдел = все без отделов видны.

---

## Rollback

Если что-то пошло не так:

1. С Мака: откатить код (git revert коммитов этой фичи или `git checkout -- .`)
2. `./deploy.sh sync && ./deploy.sh`
3. На RAG — миграцию 021 откатить не требуется (`role.tools` остались расширенные, но старый код их просто не регистрирует → LLM их не увидит, вреда нет). Если хочется чистоты — SQL:
   ```
   UPDATE roles SET tools = tools - 'user_search' WHERE code = 'pm';
   UPDATE roles SET tools = tools - 'list_documents' WHERE code = 'doc_search';
   ```

---

## Self-Review

Пробежался по spec:

- `user_search` с фильтрами name_query + role_code → **Task 2**
- `list_documents` с фильтром name_query → **Task 1**
- Visibility users = departments → **Task 2** через `get_visible_user_ids`
- Visibility docs = own+public → **Task 1** (фильтр `uploaded_by_user_id == user_id OR is_public`)
- Скрытие is_system users → **Task 2** (`not u.is_system`)
- Макс 30 записей, truncate summary до 100 символов → **Tasks 1, 2**
- Регистрация в ToolRegistry → **Task 3**
- Миграция 021 `tools ||=` → **Task 4**
- Деплой + runtime тест → **Task 5**

Pre-conditions:
- `department_service` и `user_file_storage` должны быть инициализированы до `ToolRegistry` — **Step 3.3** это проверяет
- `list_by_role` возвращает юзеров без фильтра по org — **Task 2** фильтрует вручную

Нет placeholders. Тип-сигнатуры согласованы (tool factories возвращают tuples, распаковываются в engine_service).
