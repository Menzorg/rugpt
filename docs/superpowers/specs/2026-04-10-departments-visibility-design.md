# Отделы и права видимости — спецификация

Дата: 2026-04-10

## Обзор

Организационная структура внутри организации: отделы (departments), правила видимости между отделами, контекст организации для AI-ролей.

## Модель данных

### Новые таблицы

**departments** — плоский список отделов внутри организации:
- `id` UUID PK
- `org_id` UUID FK -> organizations
- `name` VARCHAR(255) NOT NULL
- `created_at`, `updated_at` TIMESTAMPTZ

При удалении отдела: users.department_id -> NULL (ON DELETE SET NULL), правила видимости удаляются (ON DELETE CASCADE).

**department_visibility** — симметричные правила видимости между отделами:
- `id` UUID PK
- `org_id` UUID FK -> organizations
- `department_a_id` UUID FK -> departments ON DELETE CASCADE
- `department_b_id` UUID FK -> departments ON DELETE CASCADE
- `created_at` TIMESTAMPTZ
- UNIQUE(department_a_id, department_b_id)

Одна запись на пару. Симметричная: если A видит B, то B видит A. CHECK(department_a_id < department_b_id) гарантирует порядок и исключает дубликаты.

### Изменения в существующих таблицах

**users:**
- `+department_id` UUID NULL REFERENCES departments(id) ON DELETE SET NULL
- `+is_head` BOOLEAN NOT NULL DEFAULT false

**organizations:**
- `+org_context` TEXT NOT NULL DEFAULT '' — описание структуры для промптов AI-ролей

### Принципы

- Один пользователь — строго один отдел
- Пользователь без отдела (department_id NULL) — виден всем
- is_system пользователи (AI) — видны всем, вне фильтров
- Отделы плоские, без вложенности
- Без персональных overrides

## Логика видимости

Центральный метод: `DepartmentService.get_visible_user_ids(viewer_user_id, org_id) -> Set[UUID]`

Алгоритм:
1. Получить viewer (department_id, is_head, is_admin, is_system)
2. Если `is_admin` -> вернуть всех в org
3. Собрать visible_department_ids: свой отдел + отделы из department_visibility где свой отдел участвует
4. Если `is_head` -> добавить всех is_head этой организации + всех is_admin этой организации
5. Добавить всех пользователей с department_id NULL (без отдела — видны всем)
6. Добавить всех is_system (AI-пользователи — видны всем)
7. Вернуть set user_ids

`viewer_user_id` всегда берётся из JWT (`current_user["user_id"]`), никогда из параметров запроса.

## Org Context для промптов

**Хранение:** поле `org_context` (TEXT) на таблице organizations.

**Заполнение:**
- Прямой ввод текста: `PATCH /organizations/{id}` с полем org_context
- Загрузка файла: `POST /organizations/{id}/context/upload` — файл проходит через Tika, текст записывается в org_context

**Инъекция в промпт:** PromptCache.get_prompt(role) — если org_context непустой, вставить в начало system prompt перед промптом роли:
```
[org_context]

---

[role prompt]
```

## API эндпоинты

### Новый роутер `/api/v1/departments` (admin only)

| Метод | Путь | Описание |
|---|---|---|
| POST | `/` | Создать отдел |
| GET | `/` | Список отделов организации |
| GET | `/{id}` | Получить отдел |
| PATCH | `/{id}` | Обновить отдел |
| DELETE | `/{id}` | Удалить отдел |
| POST | `/{id}/head/{user_id}` | Назначить руководителя |
| DELETE | `/{id}/head` | Снять руководителя |
| POST | `/visibility` | Создать правило (dept_a_id, dept_b_id) |
| DELETE | `/visibility/{id}` | Удалить правило |
| GET | `/visibility` | Список правил организации |

### Загрузка org_context

| Метод | Путь | Описание |
|---|---|---|
| POST | `/organizations/{id}/context/upload` | Загрузить файл -> Tika -> org_context |

### Изменения в существующих эндпоинтах

- `POST /users`, `PATCH /users/{id}` — +department_id
- `POST /auth/login` response — +department_id, +is_head
- Все эндпоинты возвращающие User — +department_id, +is_head

## Фильтрация видимости в существующих эндпоинтах

| Файл | Метод | Изменение |
|---|---|---|
| routes/users.py | list_users, get_user, get_user_by_username | Фильтр по visible_user_ids |
| routes/chats.py | create_direct_chat | Проверка other_user_id видим |
| routes/chats.py | create_group_chat | Проверка всех participant_ids видимы |
| routes/chats.py | add_participant | Проверка participant_id видим |
| routes/chats.py | send_message | Передать sender_id в resolve_mentions |
| routes/tasks.py | create_task | Проверка assignee_user_id видим |
| routes/tasks.py | update_task | Проверка при смене assignee |
| routes/tasks.py | list_tasks | is_head видит задачи видимых пользователей |
| routes/roles.py | get_role_users | Фильтр по visible_user_ids |

**Невидимый пользователь -> HTTP 403** на роутах, **тихий пропуск + warning в лог** на mentions.

## Изменения в сервисах

| Файл | Метод | Изменение |
|---|---|---|
| mention_service.py | resolve_mentions | +sender_id параметр, пропуск невидимых |
| engine_service.py | initialize/close | +DepartmentStorage, +DepartmentService |
| prompt_cache.py | get_prompt | +org_context в начало промпта |

## Изменения в агентах

| Файл | Метод | Изменение |
|---|---|---|
| task_tool.py | _task_create | Проверка видимости assignee |
| task_tool.py | _task_query | Фильтр задач по видимости |

## Новые файлы

| Файл | Описание |
|---|---|
| models/department.py | Department, DepartmentVisibility dataclasses |
| storage/department_storage.py | CRUD + get_visible_user_ids() |
| services/department_service.py | Бизнес-логика + центральный метод видимости |
| routes/departments.py | API управления отделами (admin only) |
| migrations/015_departments.sql | Таблицы + ALTER users + ALTER organizations |

## Миграция 015_departments.sql

```sql
CREATE TABLE departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_departments_org_id ON departments(org_id);

CREATE TABLE department_visibility (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    department_a_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    department_b_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(department_a_id, department_b_id),
    CHECK(department_a_id < department_b_id)  -- гарантия порядка, без дубликатов
);
CREATE INDEX idx_dept_vis_a ON department_visibility(department_a_id);
CREATE INDEX idx_dept_vis_b ON department_visibility(department_b_id);

ALTER TABLE users ADD COLUMN department_id UUID REFERENCES departments(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN is_head BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_users_department_id ON users(department_id);

ALTER TABLE organizations ADD COLUMN org_context TEXT NOT NULL DEFAULT '';
```
