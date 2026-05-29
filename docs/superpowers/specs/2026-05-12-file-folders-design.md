# Папки для файлов пользователя

**Дата:** 2026-05-12
**Статус:** Утверждено (brainstorming → spec → implementation plan)
**Скоуп:** Engine (`/root/rugpt/`) + WebClient (`/root/webclient_rugpt/`)

## Цель

Дать пользователю возможность организовывать свои файлы в дерево папок: создавать, переименовывать, перемещать, удалять; вкладывать папки в папки; класть файлы в папки.

Это **только UI/UX-фича**: пользователь видит дерево вместо плоского списка. Агентные тулзы (`list_documents`, `rag_search`) в MVP не трогаем — продолжают работать как сейчас, фильтруя по `user_id + org_id`. Все интеграции с тулзами вынесены в техдолг.

## Зафиксированные решения

1. **Папки персональные.** `folders.user_id NOT NULL`. Кросс-юзер-папки не поддерживаются.
2. **Файл ↔ одна папка** (или корень = `folder_id IS NULL`).
3. **Каскадный soft-delete.** Удаление папки деактивирует всё содержимое (subtree папок + файлы).
4. **Adjacency list** для дерева (`parent_folder_id`). Recursive CTE — только в storage, в двух методах.
5. **Лимит глубины 10 уровней.** Защита от runaway: hard-cap `WHERE depth < 20` внутри recursive CTE.
6. **Имя уникально в пределах одного parent** (case-insensitive). DB partial unique index.
7. **Клон файла (`POST /files/{id}/clone`) попадает в корень получателя** (`folder_id=NULL`).
8. **owner_type=user/org откладываем.** Сейчас всё personal. Org/shared folders — TD-9.

## Архитектура

```
+----------------------------+        +-----------------------------+
| WebClient (NestJS)         |        | Engine (FastAPI)            |
|   /folders/* controller    |  HTTP  |   /folders/* router         |
|   /files/* controller (+)  |◀──────▶|   /files/* router (+)       |
|                            |        |                             |
|   adapter commands:        |        |   FolderService             |
|     create_folder          |        |   FileService (+ folder_id) |
|     get_folder_tree        |        |                             |
|     update_folder          |        |   UserFileFolderStorage     |
|     delete_folder          |        |   UserFileStorage (+)       |
|     move_file_to_folder    |        |                             |
|     upload_file (+)        |        |   PostgreSQL:               |
|     get_files (+folder_id) |        |     user_file_folders (NEW) |
+----------------------------+        |     user_files (+ folder_id)|
                                      +-----------------------------+

Frontend (Next.js):
  /files page переделана:
    - FolderTree (sidebar)
    - Breadcrumb
    - Текущая папка как URL state ?folder=<uuid>
    - Modal'ы: создать/переименовать/переместить
```

Engine — единственный источник истины. Webclient — тонкий proxy с обязательным snake↔camel mapping (см. техдолг webclient, пункт 8).

## Схема БД

Миграция `037_user_file_folders.sql`:

```sql
CREATE TABLE user_file_folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    parent_folder_id UUID REFERENCES user_file_folders(id),
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_folders_user_parent
    ON user_file_folders(user_id, parent_folder_id)
    WHERE is_active = true;

-- PG 15+ NULLS NOT DISTINCT: NULL parent_folder_id (корень) трактуется как
-- равные значения для проверки уникальности. У нас PG 16 — поддержка есть.
CREATE UNIQUE INDEX idx_folders_name_unique
    ON user_file_folders(user_id, parent_folder_id, lower(name))
    NULLS NOT DISTINCT
    WHERE is_active = true;

ALTER TABLE user_files
    ADD COLUMN folder_id UUID REFERENCES user_file_folders(id);

CREATE INDEX idx_user_files_folder
    ON user_files(user_id, folder_id)
    WHERE is_active = true;

COMMENT ON TABLE user_file_folders IS
    'Personal folders for user files. Adjacency list, soft-delete via is_active.';
COMMENT ON COLUMN user_file_folders.parent_folder_id IS
    'NULL = root level. NOT ON DELETE CASCADE — soft-delete only.';
COMMENT ON COLUMN user_files.folder_id IS
    'NULL = root level. Owner must match folder.user_id (service-enforced).';
```

**Инварианты:**

| Инвариант | Где enforced |
|---|---|
| `parent.user_id == self.user_id` | Service layer |
| `parent.org_id == self.org_id` | Service layer (defense-in-depth) |
| `user_files.folder.user_id == user_files.user_id` | Service layer |
| No cycles в дереве | Service layer: cycle-check через `list_subtree_ids` перед UPDATE |
| `depth ≤ 10` | Service layer + hard-cap `WHERE depth < 20` в recursive CTE |
| Уникальность имени в одном parent | DB partial unique index → `UniqueViolationError` → 409 |
| Soft-delete каскад | Service layer: `deactivate_subtree` + `deactivate_by_folder_ids` в одной транзакции |

**FK поведение:** все FK без `ON DELETE`. Hard-delete блокируется. Soft-delete управляется приложением.

## Engine: Models

**Новый файл `src/engine/models/user_file_folder.py`:**

```python
@dataclass
class UserFileFolder:
    id: UUID = field(default_factory=uuid4)
    user_id: UUID = field(default_factory=uuid4)
    org_id: UUID = field(default_factory=uuid4)
    parent_folder_id: Optional[UUID] = None
    name: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict: ...
```

**Изменения в `src/engine/models/user_file.py`:**
- Добавить `folder_id: Optional[UUID] = None`
- `to_dict()` включает `"folder_id": str(self.folder_id) if self.folder_id else None`

## Engine: Storage

**Новый файл `src/engine/storage/user_file_folder_storage.py` — методы:**

| Метод | Тип SQL |
|---|---|
| `create(folder)` | INSERT, ловит `UniqueViolationError` (бросает наверх) |
| `get_by_id(folder_id)` | SELECT WHERE id AND is_active |
| `list_by_user(user_id)` | SELECT … ORDER BY parent_folder_id NULLS FIRST, name |
| `list_children(user_id, parent_folder_id)` | SELECT WHERE parent_folder_id IS [NOT] DISTINCT FROM $2 |
| `list_subtree_ids(folder_id)` | **Recursive CTE** → set[UUID]; включает self |
| `update(folder)` | UPDATE name, parent_folder_id, updated_at |
| `deactivate_subtree(folder_id)` | **Recursive CTE** → UPDATE is_active=false для self+потомков, RETURNING ids |
| `get_path_strings(folder_ids)` | **Recursive CTE** → Dict[UUID → "/A/B/C"]. В MVP не зовётся, но storage-метод нужен для будущего TD-1 |

Recursive CTE-методы:

```sql
WITH RECURSIVE subtree AS (
    SELECT id, parent_folder_id, 0 AS depth
    FROM user_file_folders
    WHERE id = $1 AND is_active = true
    UNION ALL
    SELECT f.id, f.parent_folder_id, s.depth + 1
    FROM user_file_folders f
    JOIN subtree s ON f.parent_folder_id = s.id
    WHERE f.is_active = true AND s.depth < 20
)
SELECT id FROM subtree;
```

**Изменения в `src/engine/storage/user_file_storage.py`:**
- `create()` — добавить `folder_id` в INSERT (16-й параметр)
- `_row_to_file()` — читать `folder_id` (с проверкой `if "folder_id" in row.keys()` для backward-compat при чтении в тестах)
- `list_by_user_in_folder(user_id, folder_id) -> List[UserFile]` — для GET `/folders/{id}/files`. `folder_id=None` → корень
- `list_by_user_in_folder` использует `WHERE folder_id IS [NOT] DISTINCT FROM $2`
- `move_to_folder(file_id, folder_id) -> Optional[UserFile]` — UPDATE
- `deactivate_by_folder_ids(folder_ids: list[UUID]) -> int` — bulk UPDATE для каскада; возвращает число дезактивированных

## Engine: Service

**Новый файл `src/engine/services/folder_service.py`:**

```python
class FolderError(Exception):
    code: str
    message: str
    def __init__(self, code: str, message: str): ...

class FolderNotFound(FolderError): ...        # 404
class FolderForbidden(FolderError): ...       # 403
class FolderInvalidName(FolderError): ...     # 400
class FolderMaxDepthExceeded(FolderError): ...# 400
class FolderCyclicMove(FolderError): ...      # 400
class FolderInvalidParentOwner(FolderError):..# 400
class FolderParentNotFound(FolderError): ...  # 404
class FolderNameConflict(FolderError): ...    # 409

class FolderService:
    MAX_DEPTH = 10

    def __init__(
        self,
        folder_storage: UserFileFolderStorage,
        file_storage: UserFileStorage,
        rag_service: RAGService,
        storage_adapter: StorageAdapter,
    ): ...

    async def create(
        self, *, user_id: UUID, org_id: UUID,
        parent_folder_id: Optional[UUID], name: str,
    ) -> UserFileFolder

    async def rename(
        self, *, folder_id: UUID, new_name: str, actor: User,
    ) -> UserFileFolder

    async def move(
        self, *, folder_id: UUID, new_parent_id: Optional[UUID], actor: User,
    ) -> UserFileFolder

    async def delete(
        self, *, folder_id: UUID, actor: User,
    ) -> dict  # {deleted_folders: int, deleted_files: int}

    async def get_tree(
        self, *, user_id: UUID, org_id: UUID,
    ) -> list[dict]  # nested children

    async def list_children(
        self, *, user_id: UUID, parent_folder_id: Optional[UUID],
    ) -> list[UserFileFolder]

    async def list_in_folder(
        self, *, user_id: UUID, folder_id: Optional[UUID],
    ) -> list[UserFile]  # делегирует в file_storage

    # Используется FileService.upload для валидации folder_id
    async def verify_folder_owner(
        self, *, folder_id: UUID, user_id: UUID, org_id: UUID,
    ) -> UserFileFolder  # raises FolderNotFound / FolderInvalidParentOwner
```

**Бизнес-правила:**

| Операция | Проверки (порядок) |
|---|---|
| `create` | name.strip() ≠ "" → иначе `FolderInvalidName(EMPTY_NAME)`; len(name) ≤ 255 → `NAME_TOO_LONG`; если parent_folder_id задан: parent существует и active → `FolderParentNotFound`; parent.user_id == user_id, parent.org_id == org_id → `FolderInvalidParentOwner`; depth(parent)+1 ≤ MAX_DEPTH → `FolderMaxDepthExceeded`. На INSERT ловим `UniqueViolationError` → `FolderNameConflict` |
| `rename` | folder существует → `FolderNotFound`; actor.user_id == folder.user_id OR (actor.is_admin AND actor.org_id == folder.org_id) → `FolderForbidden`; name validation как в create; INSERT-конфликт ловим |
| `move` | folder + (new_parent если не None) существуют, owner check; `new_parent_id != folder_id`; `new_parent_id ∉ list_subtree_ids(folder_id)` → `FolderCyclicMove`; новая глубина ≤ MAX_DEPTH (depth(new_parent) + max(depth_in_subtree)) → `FolderMaxDepthExceeded`; conflict-check имени в новом parent |
| `delete` | folder + owner check; собрать `subtree_ids` через `folder_storage.list_subtree_ids`. Затем последовательно (без cross-pool tx — её в codebase нет): (1) `file_storage.deactivate_by_folder_ids(subtree_ids)` → возвращает file_ids дезактивированных; (2) `folder_storage.deactivate_subtree(folder_id)`. Best-effort после: для каждого indexed file → `rag_service.delete_document` (лог warning при failure); `adapter.delete(file.storage_key)` (лог warning). Идемпотентность: повторный delete на той же папке — no-op (subtree пустой) |

**Изменения в `src/engine/services/file_service.py`:**
- `upload(..., folder_id: Optional[UUID] = None)` — записывает `folder_id` в запись. **Валидация принадлежности папки делается на уровне роута до вызова upload** (route вызывает `folder_service.verify_folder_owner` если `folder_id` задан, потом `file_service.upload`). File-service сам в folder_service не ходит — это избегает circular dep
- `move_to_folder(file_id, new_folder_id, actor: User) -> UserFile` — проверяет владение файлом (`actor.user_id == file.user_id` или admin). Валидация целевой папки — тоже на уровне роута перед вызовом
- `clone()` — без изменений семантики: клон всегда в корень получателя (`folder_id=None` в новой записи)

**Изменения в `src/engine/services/engine_service.py`:**
- Добавить `folder_storage: UserFileFolderStorage` в storage список
- Добавить `folder_service: FolderService` после `rag_service` и `file_service` в порядке инициализации (зависит от обоих storage + rag_service + adapter)
- Никаких циклических ссылок между `folder_service` и `file_service`: они независимы, координация через route layer

## Engine: API

**Новый роутер `src/engine/routes/folders.py` (prefix `/folders`):**

| Method | Endpoint | Body / Query | Назначение |
|---|---|---|---|
| POST | `/folders` | `{name: str, parent_folder_id?: UUID}`, `?user_id=` (admin only) | Создать папку |
| GET | `/folders` | `?user_id=` (admin only) | Плоский список папок (для построения дерева на фронте) |
| GET | `/folders/tree` | `?user_id=` (admin only) | Уже собранное дерево с `children` |
| GET | `/folders/{folder_id}` | — | Метаданные одной папки |
| PATCH | `/folders/{folder_id}` | `{name?: str, parent_folder_id?: UUID \| null}` | Rename и/или move в одном запросе |
| DELETE | `/folders/{folder_id}` | — | Каскадный soft-delete; response `{deleted_folders, deleted_files}` |
| GET | `/folders/{folder_id}/files` | — | Файлы в этой папке (не subtree) |

**Изменения в `src/engine/routes/files.py`:**

| Endpoint | Изменение |
|---|---|
| POST `/files/upload` | Form-поле `folder_id?: str` |
| GET `/files` | Query `?folder_id=` (либо UUID, либо строка `null` для корня; отсутствие = все файлы как сейчас) |
| PATCH `/files/{file_id}/folder` | **Новый.** Body: `{folder_id: UUID \| null}`. Перенос файла |

**Маппинг FolderError → HTTP:**

В роутере один helper:

```python
def _folder_error_to_http(e: FolderError) -> HTTPException:
    status_map = {
        "EMPTY_NAME": 400, "NAME_TOO_LONG": 400,
        "MAX_DEPTH_EXCEEDED": 400, "CYCLIC_MOVE": 400,
        "INVALID_PARENT_OWNER": 400,
        "FORBIDDEN": 403,
        "FOLDER_NOT_FOUND": 404, "PARENT_NOT_FOUND": 404,
        "DUPLICATE_NAME": 409,
    }
    return HTTPException(
        status_code=status_map.get(e.code, 500),
        detail={"code": e.code, "message": e.message},
    )
```

**Auth:**
- Все endpoints под `Depends(get_current_user)` (JWT)
- Mutations (POST/PATCH/DELETE) проверяются `SignatureGuard` на стороне NestJS перед проксированием
- admin-доступ через `current_user.is_admin AND folder.org_id == current_user.org_id`

**Response shapes:**

```json
// GET /folders/{id}, POST /folders
{
  "id": "uuid",
  "user_id": "uuid",
  "org_id": "uuid",
  "parent_folder_id": "uuid | null",
  "name": "Договоры",
  "is_active": true,
  "created_at": "2026-05-12T10:00:00",
  "updated_at": "2026-05-12T10:00:00"
}

// GET /folders/tree
[
  {
    "id": "uuid", "name": "Договоры", "parent_folder_id": null,
    "children": [{"id": "...", "name": "2024", "parent_folder_id": "...", "children": []}]
  }
]

// DELETE /folders/{id}
{"success": true, "deleted_folders": 4, "deleted_files": 18}

// Errors (4xx)
{"detail": {"code": "MAX_DEPTH_EXCEEDED", "message": "Превышена максимальная глубина вложенности (10 уровней)"}}
```

## WebClient: Backend (NestJS)

**Новый модуль `packages/backend/src/folder/`:**
- `folder.controller.ts` — REST под `/folders` с `@UseGuards(JwtAuthGuard)`; mutations без `@SkipSignature`, чтения с `@SkipSignature`
- `folder.service.ts` — проксирует через `engineAdapter.execute()`, делает явный `mapFolder` (snake→camel)
- `folder.module.ts` + регистрация в `app.module.ts`

**Изменения в `RuGPTEngineAdapter` (`engine/adapters/rugpt.adapter.ts`):**

Новые `case` в `execute(command, payload)`:

| Команда | HTTP к engine |
|---|---|
| `create_folder` | `POST /api/v1/folders` |
| `list_folders` | `GET /api/v1/folders[?user_id=]` |
| `get_folder_tree` | `GET /api/v1/folders/tree[?user_id=]` |
| `get_folder` | `GET /api/v1/folders/{id}` |
| `update_folder` | `PATCH /api/v1/folders/{id}` |
| `delete_folder` | `DELETE /api/v1/folders/{id}` |
| `list_folder_files` | `GET /api/v1/folders/{id}/files` |
| `move_file_to_folder` | `PATCH /api/v1/files/{id}/folder` |

Обновлённые:
- `upload_file` — добавить `folder_id` в FormData если задан
- `get_files` — пробросить query `?folder_id=`

**Mapping (folder.service.ts):**

```typescript
private mapFolder(data: any): any {
  return {
    id: data.id,
    userId: data.user_id,
    orgId: data.org_id,
    parentFolderId: data.parent_folder_id,
    name: data.name,
    isActive: data.is_active,
    createdAt: data.created_at,
    updatedAt: data.updated_at,
  };
}
private mapFolderTreeNode(node: any): any {
  return {
    ...this.mapFolder(node),
    children: (node.children || []).map((c: any) => this.mapFolderTreeNode(c)),
  };
}
```

**Изменения в `file.service.ts`:**
- `mapFile()` дополнить `folderId: data.folder_id` (сейчас этого поля нет — это закроет потенциальный snake↔camel баг для нового поля)
- `upload()` — принять опциональный `folderId`, передать в FormData
- Новый метод `moveToFolder(fileId, folderId | null, currentUser)`

**Shared types (`packages/common/src/types/`):**
- Добавить `Folder` и `FolderTreeNode` интерфейсы (camelCase)
- Расширить `File` интерфейс полем `folderId: string | null`

## WebClient: Frontend (Next.js)

**Изменения в `packages/frontend/src/app/files/page.tsx`:**

Концепции:
- **Current folder** — URL-state `?folder=<uuid>` либо отсутствует (корень)
- **Folder tree** в sidebar
- **Breadcrumb** сверху main area

Структура (схематично):
```
+-----------------------------------------------------+
|  ChatNavigation: "Файлы"                            |
+--------------+--------------------------------------+
|  FolderTree  |  Breadcrumb: / > Договоры > 2024     |
|              |  [+ Папка]  [+ Файл]                  |
|              |                                      |
|              |  Список текущей папки:               |
|              |  - 📁 Q1                             |
|              |  - 📁 Q2                             |
|              |  - 📄 НДА-001.pdf  [⋮]               |
+--------------+--------------------------------------+
```

**Новые компоненты:**
- `FolderTree.tsx` — рекурсивный рендер дерева; click → `router.push('?folder=...')`
- `Breadcrumb.tsx` — строится из folder.parentFolderId chain (берётся из tree)
- `CreateFolderModal.tsx`
- `RenameFolderModal.tsx`
- `MoveModal.tsx` — выбор папки назначения (для папки или файла); рендерит FolderTree в режиме picker'а

**Новый хук `useFolders.ts`:**
- `fetchTree()` — запрос `GET /folders/tree`, кеш в state
- `createFolder({name, parentFolderId})`, `renameFolder({id, name})`, `moveFolder({id, parentFolderId})`, `deleteFolder({id})`
- Все операции пере-fetch'ат tree после успеха (простой подход; оптимистичные апдейты — в техдолг)

**Расширение `useFiles.ts`:**
- `moveFileToFolder(fileId, folderId | null)`
- `fetchFiles({folderId?: string | null})` — фильтрует на серверной стороне
- `uploadFile({...existing, folderId?: string | null})`

**Error handling:**
- Получаем `error.response.data.detail = {code, message}` из engine через webclient
- 409 `DUPLICATE_NAME` — inline-ошибка в modal'е под полем имени
- 400 `CYCLIC_MOVE` / `MAX_DEPTH_EXCEEDED` — toast с `message`
- 403 `FORBIDDEN` — toast «Доступ запрещён»
- Сообщения отображаются как есть (engine возвращает русские строки); фронт-локализация через `code`-mapping — в будущем

**URL-state:**
- `/files` → корень
- `/files?folder=<uuid>` → текущая папка
- Browser back/forward работают через `useSearchParams`

**Что НЕ в MVP** (всё в техдолг):
- Drag-n-drop файла на папку в дереве (только через MoveModal)
- Drag-n-drop из ОС напрямую в нужную папку
- Multi-select для bulk
- Оптимистичные апдейты
- Восстановление soft-deleted
- Сортировка кастомная (только алфавитная)

## Edge cases

| Сценарий | Поведение |
|---|---|
| Файл с `is_public=true` лежит в папке владельца | Другие юзеры видят файл только через RAG/чат/admin-list. UI чужих папок никто не видит |
| Клон `POST /files/{id}/clone` | Клон создаётся в корне получателя (`folder_id=NULL`). Юзер потом перемещает в нужную папку |
| RAG-pipeline | Без изменений. `chunks`, `tables_rows_chunks`, поиск через pgvector — папка не используется. Соответствие WHERE-фильтрам остаётся `org_id + (is_public OR user_id == viewer)` |
| Существующие файлы до миграции | `folder_id = NULL` после `ALTER TABLE`. Все «в корне». Поведение совместимо с текущим UI после первого деплоя |
| Admin открывает `/files` | Флэт-список как сейчас. Через query `?user_id=` admin может посмотреть дерево конкретного юзера. UI: simple dropdown «Смотреть от лица: ...» |
| Soft-deleted папка | `is_active=false`. Скрыта везде. Unique constraint партиальный (`WHERE is_active=true`) → имя освобождается, можно создать новую с тем же именем. Восстановление — техдолг |
| Race на имя | DB unique partial index → `UniqueViolationError` → 409 |
| Move папки в себя или в свой subtree | Service-check (`new_parent ∈ list_subtree_ids(folder_id)`) → 400 `CYCLIC_MOVE` |
| Move привёл бы к depth > 10 | 400 `MAX_DEPTH_EXCEEDED` |
| Engine упал во время cascade-delete | Последовательно: files-deactivate → folders-deactivate (cross-pool tx в codebase отсутствует). Если упали между: файлы soft-deleted (скрыты из UI), папки ещё active (показываются пустыми). Юзер повторяет delete → step 1 no-op (files уже inactive), step 2 завершает. Чтения корректны и в промежуточном состоянии: list-by-folder фильтрует по `is_active=true` файлов; list-folder-tree скроет ту папку при её deactivate. Orphan-байты диска / orphan-чанки RAG — TD-8 (periodic cleanup) |
| RAG-cleanup упал | Лог warning, операция не откатывается. Чанки удалённого файла остаются как orphan, но WHERE-фильтр `is_active=true` на user_files отфильтрует их из любых результатов |
| Эндпоинт `/files` (без folder_id query) | Полный список всех файлов юзера (как сейчас) — backward-compat. Старые клиенты, не знающие про папки, продолжат работать |

## Тестирование

**Unit (Python):**
- `folder_storage_test.py` — recursive CTE на тривиальных и глубоких деревьях; cycle detection возвращает корректный subtree
- `folder_service_test.py` (mock storage) — все валидации: empty_name, depth, cycle, owner_check, parent_owner_check, name_conflict
- `file_service_test.py` — `upload(folder_id=...)`, `move_to_folder` с верными/неверными folder_id, clone остаётся в root

**Integration (real PG):**
- Создание дерева 5 уровней; `get_tree` возвращает корректную вложенность
- `delete` каскадно: 3 уровня папок + 10 файлов → DB записи `is_active=false`; mock rag_service вызывается ровно для indexed-файлов
- Unique constraint: concurrent INSERT двух папок с одинаковым именем — одна 200, вторая `UniqueViolationError` → 409
- Move с `CYCLIC_MOVE`
- Transaction rollback: artificial fault посреди cascade — БД консистентна

**API (FastAPI):**
- Smoke на каждый endpoint: 200 + response shape match
- 401/403 auth checks
- 400/404/409 error code matches table

**WebClient backend (Jest):**
- `folder.service.spec.ts` — `mapFolder` корректен на образцовых snake_case ответах
- adapter command routing

**Frontend:**
- Component-tests для `FolderTree.tsx`: рендер вложенного списка, click handler меняет URL
- Manual golden path: create → upload → move → delete cascade → all gone в UI

## Техдолг (новая секция в `tech-debt.md` обоих репозиториев)

«Файловые папки — отложенные улучшения»:

| # | Идея | Зачем | Оценка |
|---|---|---|---|
| TD-1 | Folder path в `list_documents` (engine tool) | LLM видит `/Договоры/2024/НДА.pdf`. Лучший контекст, меньше галлюцинаций | ~30 строк: batch-load папок через `folder_storage.get_path_strings()` + format-строка |
| TD-2 | Фильтр `list_documents` по `folder_id` | LLM может сужать поиск «в папке X» | ~50 строк: optional param + `folder_storage.list_subtree_ids` |
| TD-3 | Новый tool `get_directory_tree` | LLM получает ASCII-карту дерева (с file_count, опц. файлами в листьях) | ~50 строк: новый `tools/get_directory_tree.py` + регистрация в ToolRegistry |
| TD-4 | `rag_search` scoped to folder | Семантический поиск ограничен subtree. Риск регрессии RAG → отложено | ~80 строк pre-filter в Python, ~150 при SQL-уровне |
| TD-5 | Drag-n-drop в UI | Лучший UX вместо MoveModal | ~100 строк frontend |
| TD-6 | Bulk operations | Multi-select + bulk move/delete | Frontend selection state + bulk endpoints в engine |
| TD-7 | Восстановление soft-deleted | «Trash bin» с undo в течение N дней | Reactivate service-метод + UI `/trash` |
| TD-8 | Periodic cleanup orphan storage bytes | Cron-job удаляет физические файлы где `is_active=false older than 30 days` | Scheduler-task |
| TD-9 | Public/shared org folders | Папки, видимые всем в org. Требует отдельного дизайна (permission model, UI integration, name conflicts) | Полноценная вторая фича — новый brainstorming session |
| TD-10 | Folder reordering | Кастомный порядок папок (колонка `order_index`) | Migration + drag handle |
