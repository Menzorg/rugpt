# Управление проектами (CRUD + видимость) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вкладка «Проекты» в `/tasks` с полным CRUD; любой создаёт проект, правят/удаляют создатель+admin+head-своего-отдела; видимость списка скоупится (admin → орга, head → свой отдел + личное участие, обычный → личное участие). Отдел проекта хранится в `projects.department_id` (заморожен при создании).

**Architecture:** Engine: новая колонка `projects.department_id` (backfill из отдела создателя), права в `ProjectService` по хранимому полю (без чтения создателя), per-viewer видимость в storage. NestJS — только +поле в маппинге. Frontend: `useProjects` +update/remove, вкладка «Проекты» (отдельный компонент) с гейтом и бейджем «другой отдел».

**Tech Stack:** Python/FastAPI/asyncpg, pytest (mock-idiom как в `tests/test_projects.py`). NestJS/TS. Next.js/React/Tailwind. `@webchat/common`.

**Спека:** `docs/superpowers/specs/2026-05-21-projects-management-design.md`.

**Правила проекта:** git — только вручную пользователем. Шаги «Checkpoint» — точка ручного коммита; агент `git` не вызывает.

**Команды проверки:**
- Engine: `cd /root/rugpt && venv/bin/python -m pytest <path> -v`
- common build: `cd /root/webclient_rugpt/packages/common && npm run build`
- backend build: `cd /root/webclient_rugpt/packages/backend && npm run build`
- frontend typecheck/build: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit` затем `npm run build`

---

## Файловая карта

**Engine (`/root/rugpt`):**
- Create: `src/engine/migrations/046_project_department.sql`
- Modify: `src/engine/models/project.py` (поле + to_dict)
- Modify: `src/engine/storage/project_storage.py` (create INSERT, `_row_to_project`, новый `list_visible_for_user`)
- Modify: `src/engine/services/project_service.py` (create, `_check_can_modify`, update, delete, `list_visible`)
- Modify: `src/engine/routes/projects.py` (`GET /projects` → per-viewer)
- Tests: `tests/test_projects.py` (правка существующих + новые), `tests/test_project_storage_visibility.py` (новый, SQL-assert)

**common:** `packages/common/src/types/project.ts` (+departmentId)

**NestJS:** `packages/backend/src/project/project.service.ts` (mapProject +departmentId)

**Frontend:**
- Modify: `packages/frontend/src/app/hooks/useProjects.ts` (+update/remove, +departmentId)
- Create: `packages/frontend/src/app/components/ProjectsTab.tsx`
- Modify: `packages/frontend/src/app/tasks/page.tsx` (топ-вкладка «Проекты» + рендер ProjectsTab + ослабить инлайн-гейт «+»)

---

## Task 1: Engine — миграция 046 (колонка + backfill)

**Files:**
- Create: `src/engine/migrations/046_project_department.sql`

> Без юнит-теста (SQL-миграция). Проверка — синтаксис файла; реальное применение делает пользователь на проде.

- [ ] **Step 1: Создать миграцию**

```sql
-- 046_project_department.sql
-- Adds projects.department_id (owning department, frozen at creation = creator's
-- department). Backfills existing rows from the creator's current department.
-- Idempotent.

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS department_id UUID NULL
  REFERENCES departments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_projects_department
  ON projects(department_id) WHERE department_id IS NOT NULL;

UPDATE projects p
   SET department_id = u.department_id
  FROM users u
 WHERE u.id = p.created_by_user_id
   AND p.department_id IS NULL;
```

- [ ] **Step 2: Проверить, что файл — валидный SQL глазами** (нет точек с запятой внутри, FK на `departments(id)` корректен; `departments` существует с миграции 015).

- [ ] **Step 3: Checkpoint** (применение миграции — на проде пользователем; не запускаем тут).

---

## Task 2: Engine — провести `department_id` через модель и storage

**Files:**
- Modify: `src/engine/models/project.py`
- Modify: `src/engine/storage/project_storage.py`
- Test: `tests/test_project_model.py` (новый)

- [ ] **Step 1: Failing test**

Создать `/root/rugpt/tests/test_project_model.py`:
```python
from uuid import uuid4
from src.engine.models.project import Project


def test_to_dict_includes_department_id():
    dep = uuid4()
    p = Project(name="P", department_id=dep)
    d = p.to_dict()
    assert d["department_id"] == str(dep)


def test_to_dict_department_id_none():
    p = Project(name="P")  # default department_id None
    assert p.to_dict()["department_id"] is None
```

- [ ] **Step 2: Run, expect FAIL**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_project_model.py -v`
Expected: FAIL — `Project` has no `department_id` (TypeError on kwarg) / key missing.

- [ ] **Step 3: Model — add field + to_dict key**

В `src/engine/models/project.py`, в dataclass добавить поле после `created_by_user_id`:
```python
    created_by_user_id: Optional[UUID] = None
    department_id: Optional[UUID] = None
```
В `to_dict` добавить ключ (после `created_by_user_id`):
```python
            "created_by_user_id": str(self.created_by_user_id) if self.created_by_user_id else None,
            "department_id": str(self.department_id) if self.department_id else None,
```

- [ ] **Step 4: Storage — INSERT + row mapping**

В `src/engine/storage/project_storage.py` `create`: расширить INSERT на колонку `department_id` (`$9`):
```python
        query = """
            INSERT INTO projects
                (id, org_id, name, description, created_by_user_id,
                 is_active, created_at, updated_at, department_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
        """
        row = await self.fetchrow(
            query,
            project.id, project.org_id, project.name, project.description,
            project.created_by_user_id, project.is_active,
            project.created_at, project.updated_at, project.department_id,
        )
        return self._row_to_project(row)
```
В `_row_to_project` добавить:
```python
            created_by_user_id=row["created_by_user_id"],
            department_id=row["department_id"],
```
(`update` НЕ трогаем — `department_id` заморожен, апдейт меняет только name/description/is_active.)

- [ ] **Step 5: Run, expect PASS**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_project_model.py -v`
Expected: PASS.

- [ ] **Step 6: Checkpoint**

---

## Task 3: Engine — права (`project_service.py`)

**Files:**
- Modify: `src/engine/services/project_service.py`
- Test: `tests/test_projects.py` (заменить устаревшие + добавить)

- [ ] **Step 1: Implement — create + _check_can_modify + update/delete**

В `create(...)`: удалить строку `self._check_can_create(user)`; в конструктор `Project(...)` добавить `department_id=user.department_id`:
```python
        project = Project(
            org_id=user.org_id,
            name=name.strip(),
            description=description,
            created_by_user_id=user.id,
            department_id=user.department_id,
        )
```
Удалить метод `_check_can_create`. Добавить новый helper (синхронный):
```python
    def _check_can_modify(self, project: Project, user: User) -> None:
        """Creator, admin (whole org), or head of the project's department."""
        if user.is_admin:
            return
        if project.created_by_user_id == user.id:
            return
        if (user.is_head and user.department_id is not None
                and project.department_id == user.department_id):
            return
        raise PermissionError(
            "Only the creator, the head of the project's department, or an admin "
            "can modify this project"
        )
```
`update(...)` — переставить порядок (загрузка ПЕРЕД проверкой) и звать `_check_can_modify`:
```python
    async def update(self, project_id, user, name=None, description=None):
        project = await self.storage.get_by_id(project_id)
        if project is None:
            raise ValueError(f"Project {project_id} not found")
        self._check_same_org(project, user)
        self._check_can_modify(project, user)
        if name is not None:
            stripped = name.strip()
            if not stripped:
                raise ValueError("Project name cannot be empty")
            project.name = stripped
        if description is not None:
            project.description = description
        return await self.storage.update(project)
```
`delete(...)`:
```python
    async def delete(self, project_id, user) -> bool:
        project = await self.storage.get_by_id(project_id)
        if project is None:
            return False
        self._check_same_org(project, user)
        self._check_can_modify(project, user)
        await self.storage.deactivate(project_id)
        if self.chat_service is not None:
            await self.chat_service.archive_project_chat(project_id)
        logger.info(f"Project {project_id} archived by {user.id}")
        return True
```

- [ ] **Step 2: Update tests in `tests/test_projects.py`**

Прочитать файл. Заменить `test_create_requires_head_or_admin` (создание больше НЕ требует head/admin) и привести permission-тесты update/delete к новому правилу. Конкретно — заменить/добавить функции (helper `make_user` уже принимает `is_admin`/`is_head`; расширить вызовы `department_id` где нужно):

```python
def test_create_allowed_for_any_user():
    async def go():
        svc, storage, _ = make_service()
        storage.create = AsyncMock(side_effect=lambda p: p)
        dep = uuid4()
        user = User(id=uuid4(), org_id=uuid4(), name="u", username="u", email="u@u",
                    department_id=dep)
        p = await svc.create("P", user)
        assert p.name == "P"
        # department_id frozen from creator
        assert storage.create.call_args[0][0].department_id == dep
    asyncio.run(go())


def test_modify_by_creator_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        creator = User(id=uuid4(), org_id=org, name="c", username="c", email="c@u")
        proj = Project(org_id=org, name="P", created_by_user_id=creator.id)
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        out = await svc.update(proj.id, creator, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_admin_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        admin = User(id=uuid4(), org_id=org, name="a", username="a", email="a@u", is_admin=True)
        out = await svc.update(proj.id, admin, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_head_same_department_ok():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4(); dep = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=dep)
        storage.get_by_id = AsyncMock(return_value=proj)
        storage.update = AsyncMock(side_effect=lambda p: p)
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=dep)
        out = await svc.update(proj.id, head, name="P2")
        assert out.name == "P2"
    asyncio.run(go())


def test_modify_by_head_other_department_forbidden():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=uuid4())  # different dept
        import pytest as _pt
        with _pt.raises(PermissionError):
            await svc.update(proj.id, head, name="P2")
    asyncio.run(go())


def test_modify_by_unrelated_user_forbidden():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        proj = Project(org_id=org, name="P", created_by_user_id=uuid4(), department_id=uuid4())
        storage.get_by_id = AsyncMock(return_value=proj)
        other = User(id=uuid4(), org_id=org, name="o", username="o", email="o@u")
        import pytest as _pt
        with _pt.raises(PermissionError):
            await svc.update(proj.id, other, name="P2")
    asyncio.run(go())
```
Убедиться, что `Project` импортирован в тест-файле (он уже импортируется). **Удалить только `test_create_requires_head_or_admin`** — оно ждёт `PermissionError` для обычного юзера, а создание теперь разрешено всем (заменяется на `test_create_allowed_for_any_user`). `test_create_head_can` / `test_create_admin_can` ОСТАВИТЬ — head/admin по-прежнему могут создавать, тесты валидны. Старые permission-тесты на `update`/`delete` (если в файле есть и они опираются на `_check_can_create`/head-admin-only) — заменить новыми из этого шага.

- [ ] **Step 3: Run, expect PASS**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_projects.py -v`
Expected: PASS (старые permission-тесты обновлены, новые зелёные).

- [ ] **Step 4: Checkpoint**

---

## Task 4: Engine — видимость списка (storage + service + route)

**Files:**
- Modify: `src/engine/storage/project_storage.py` (новый `list_visible_for_user`)
- Modify: `src/engine/services/project_service.py` (новый `list_visible`)
- Modify: `src/engine/routes/projects.py` (`GET /projects`)
- Test: `tests/test_project_storage_visibility.py` (новый, SQL-assert), `tests/test_projects.py` (service routing)

- [ ] **Step 1: Failing test — storage SQL**

Создать `/root/rugpt/tests/test_project_storage_visibility.py`:
```python
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

from src.engine.storage.project_storage import ProjectStorage


@pytest.mark.asyncio
async def test_list_visible_for_user_sql_no_department():
    storage = ProjectStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    await storage.list_visible_for_user(uuid4(), uuid4(), include_archived=False)
    sql = storage.fetch.call_args.args[0]
    assert "p.created_by_user_id = $1" in sql
    assert "task_participants tp" in sql
    assert "t.assignee_user_id = $1" in sql
    assert "$4::uuid IS NOT NULL AND p.department_id = $4" in sql


@pytest.mark.asyncio
async def test_list_visible_for_user_passes_department():
    storage = ProjectStorage("postgresql://test")
    storage.fetch = AsyncMock(return_value=[])
    uid, org, dep = uuid4(), uuid4(), uuid4()
    await storage.list_visible_for_user(uid, org, include_archived=True, department_id=dep)
    args = storage.fetch.call_args.args
    # positional bind args: sql, $1=uid, $2=org, $3=include_archived, $4=department_id
    assert args[1] == uid
    assert args[2] == org
    assert args[3] is True
    assert args[4] == dep
```

- [ ] **Step 2: Run, expect FAIL**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_project_storage_visibility.py -v`
Expected: FAIL — `list_visible_for_user` не существует.

- [ ] **Step 3: Implement storage method**

В `src/engine/storage/project_storage.py` добавить:
```python
    async def list_visible_for_user(
        self,
        user_id: UUID,
        org_id: UUID,
        include_archived: bool = False,
        department_id: Optional[UUID] = None,
    ) -> List[Project]:
        """Projects visible to a regular user (or head, if department_id given):
        created by them, OR their department's projects (head), OR projects with
        an active task where they are creator/assignee/participant."""
        query = """
            SELECT * FROM projects p
            WHERE p.org_id = $2
              AND (p.is_active OR $3)
              AND (
                p.created_by_user_id = $1
                OR ($4::uuid IS NOT NULL AND p.department_id = $4)
                OR p.id IN (
                  SELECT DISTINCT t.project_id FROM tasks t
                  WHERE t.is_active AND t.project_id IS NOT NULL
                    AND (
                      t.created_by_user_id = $1
                      OR t.assignee_user_id = $1
                      OR EXISTS (SELECT 1 FROM task_participants tp
                                 WHERE tp.task_id = t.id AND tp.user_id = $1)
                    )
                )
              )
            ORDER BY p.created_at DESC
        """
        rows = await self.fetch(query, user_id, org_id, include_archived, department_id)
        return [self._row_to_project(r) for r in rows]
```

- [ ] **Step 4: Run storage test, expect PASS**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_project_storage_visibility.py -v`
Expected: PASS.

- [ ] **Step 5: Failing test — service routing** (добавить в `tests/test_projects.py`)

```python
def test_list_visible_admin_lists_whole_org():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        storage.list_by_org = AsyncMock(return_value=[])
        storage.list_visible_for_user = AsyncMock(return_value=[])
        admin = User(id=uuid4(), org_id=org, name="a", username="a", email="a@u", is_admin=True)
        await svc.list_visible(admin, include_archived=False)
        storage.list_by_org.assert_awaited_once()
        storage.list_visible_for_user.assert_not_awaited()
    asyncio.run(go())


def test_list_visible_head_passes_department():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4(); dep = uuid4()
        storage.list_visible_for_user = AsyncMock(return_value=[])
        head = User(id=uuid4(), org_id=org, name="h", username="h", email="h@u",
                    is_head=True, department_id=dep)
        await svc.list_visible(head, include_archived=False)
        kwargs = storage.list_visible_for_user.call_args.kwargs
        assert kwargs.get("department_id") == dep
    asyncio.run(go())


def test_list_visible_regular_no_department():
    async def go():
        svc, storage, _ = make_service()
        org = uuid4()
        storage.list_visible_for_user = AsyncMock(return_value=[])
        reg = User(id=uuid4(), org_id=org, name="r", username="r", email="r@u")
        await svc.list_visible(reg, include_archived=False)
        kwargs = storage.list_visible_for_user.call_args.kwargs
        assert kwargs.get("department_id") is None
    asyncio.run(go())
```

- [ ] **Step 6: Implement service method**

В `src/engine/services/project_service.py` добавить:
```python
    async def list_visible(self, user: User, include_archived: bool = False) -> List[Project]:
        if user.is_admin:
            return await self.storage.list_by_org(user.org_id, include_archived)
        department_id = user.department_id if (user.is_head and user.department_id is not None) else None
        return await self.storage.list_visible_for_user(
            user.id, user.org_id, include_archived, department_id=department_id,
        )
```

- [ ] **Step 7: Implement route**

В `src/engine/routes/projects.py` `list_projects`:
```python
@router.get("")
async def list_projects(
    include_archived: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    """List projects visible to the current user (per-viewer scope)."""
    engine = get_engine_service()
    user = await _load_user(engine, current_user["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    projects = await engine.project_service.list_visible(user, include_archived)
    return [p.to_dict() for p in projects]
```

- [ ] **Step 8: Run all engine project tests**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_projects.py tests/test_project_model.py tests/test_project_storage_visibility.py -v`
Expected: PASS.

- [ ] **Step 9: Smoke-import** `cd /root/rugpt && venv/bin/python -c "from src.engine.app import app; print('ok')"` → `ok`.

- [ ] **Step 10: Checkpoint**

---

## Task 5: common — `Project` тип + сборка

**Files:**
- Modify: `packages/common/src/types/project.ts`

- [ ] **Step 1: Add field**

В `interface Project` добавить после `createdByUserId`:
```ts
  createdByUserId?: string | null;
  departmentId?: string | null;
```

- [ ] **Step 2: Build common**

Run: `cd /root/webclient_rugpt/packages/common && npm run build`
Expected: успешно; в `dist/types/project.d.ts` есть `departmentId`.

- [ ] **Step 3: Checkpoint**

---

## Task 6: NestJS — `mapProject` + сборка

**Files:**
- Modify: `packages/backend/src/project/project.service.ts`

- [ ] **Step 1: Add field to mapProject**

В `mapProject`, после `createdByUserId`:
```ts
      createdByUserId: d.created_by_user_id,
      departmentId: d.department_id ?? null,
```

- [ ] **Step 2: Build backend**

Run: `cd /root/webclient_rugpt/packages/backend && npm run build`
Expected: успешно.

- [ ] **Step 3: Checkpoint**

---

## Task 7: Frontend — `useProjects` (update/remove + departmentId)

**Files:**
- Modify: `packages/frontend/src/app/hooks/useProjects.ts`

> Фронт без юнит-инфры → проверка `npx tsc --noEmit`.

- [ ] **Step 1: Add departmentId to local Project + update/remove**

В локальном `interface Project` добавить `departmentId?: string | null;` (после `createdByUserId`).
Добавить в хук перед `useEffect`:
```ts
  const update = useCallback(async (id: string, data: { name?: string; description?: string }) => {
    if (!token) return null;
    const api = getApiClient();
    const p = await api.signedPatch<Project>(`/api/projects/${id}`, data as unknown as Record<string, unknown>, user?.id);
    setProjects((prev) => prev.map((x) => (x.id === id ? p : x)));
    return p;
  }, [token, user?.id]);

  const remove = useCallback(async (id: string) => {
    if (!token) return;
    const api = getApiClient();
    await api.signedDelete(`/api/projects/${id}`, user?.id);
    setProjects((prev) => prev.filter((x) => x.id !== id));
  }, [token, user?.id]);
```
В `return { ... }` добавить `update, remove`:
```ts
  return { projects, loading, refetch: fetch, create, update, remove };
```

- [ ] **Step 2: Typecheck**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 3: Checkpoint**

---

## Task 8: Frontend — вкладка «Проекты» (компонент + интеграция + ослабить инлайн-гейт)

**Files:**
- Create: `packages/frontend/src/app/components/ProjectsTab.tsx`
- Modify: `packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Создать `ProjectsTab.tsx`**

Самодостаточный компонент: список проектов, создание/редактирование (модалка), архивирование, бейдж «другой отдел». Гейт по самому-себе из `users`.
```tsx
'use client';

import { useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useProjects } from '../hooks/useProjects';
import { useUsers } from '../hooks/useUsers';
import { useDepartments } from '../hooks/useDepartments';

interface Props {
  currentUserId?: string;
  isAdmin?: boolean;
}

export function ProjectsTab({ currentUserId, isAdmin }: Props) {
  const router = useRouter();
  const { projects, loading, create, update, remove } = useProjects();
  const { users } = useUsers();
  const { departments } = useDepartments();
  const deptMap = useMemo(() => new Map(departments.map((d) => [d.id, d.name])), [departments]);

  const me = useMemo(() => users.find((u) => u.id === currentUserId), [users, currentUserId]);
  const myDept = me?.departmentId ?? null;
  const iAmHead = !!me?.isHead;

  const [showCreate, setShowCreate] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);
  const [form, setForm] = useState({ name: '', description: '' });
  const [busy, setBusy] = useState(false);

  const canModify = (p: { createdByUserId?: string | null; departmentId?: string | null }) =>
    !!isAdmin
    || (!!currentUserId && p.createdByUserId === currentUserId)
    || (iAmHead && !!myDept && p.departmentId === myDept);

  const openCreate = () => { setEditId(null); setForm({ name: '', description: '' }); setShowCreate(true); };
  const openEdit = (p: { id: string; name: string; description?: string | null }) => {
    setEditId(p.id); setForm({ name: p.name, description: p.description ?? '' }); setShowCreate(true);
  };

  const submit = async () => {
    if (!form.name.trim() || busy) return;
    setBusy(true);
    try {
      if (editId) {
        await update(editId, { name: form.name.trim(), description: form.description.trim() || undefined });
      } else {
        await create({ name: form.name.trim(), description: form.description.trim() || undefined });
      }
      setShowCreate(false);
    } finally {
      setBusy(false);
    }
  };

  const archive = async (id: string) => {
    if (!confirm('Архивировать проект?')) return;
    await remove(id);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={openCreate}
          className="px-3 py-1.5 text-sm bg-primary text-white rounded hover:bg-primary-dark"
        >
          Создать проект
        </button>
      </div>

      {loading ? (
        <div className="text-neutral-dark-medium dark:text-gray-400">Загрузка…</div>
      ) : projects.length === 0 ? (
        <div className="text-center text-neutral-dark-medium dark:text-gray-400 py-8">Проектов пока нет.</div>
      ) : (
        <div className="flex flex-col gap-2">
          {projects.map((p) => {
            const otherDept = p.departmentId && p.departmentId !== myDept;
            return (
              <div key={p.id} className="p-4 rounded-card bg-neutral-light-medium dark:bg-gray-700 flex justify-between items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-neutral-dark-darkest dark:text-white truncate flex items-center gap-2">
                    {p.name}
                    {otherDept && (
                      <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-200 dark:bg-amber-900/40 text-amber-900 dark:text-amber-200">
                        Другой отдел{deptMap.get(p.departmentId as string) ? `: ${deptMap.get(p.departmentId as string)}` : ''}
                      </span>
                    )}
                  </div>
                  {p.description && (
                    <div className="text-xs text-neutral-dark-medium dark:text-gray-400 mt-1 line-clamp-2">{p.description}</div>
                  )}
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <button type="button" onClick={() => router.push(`/chat/project/${p.id}`)} className="text-sm text-primary underline">Открыть чат</button>
                  {canModify(p) && (
                    <div className="flex gap-2">
                      <button type="button" onClick={() => openEdit(p)} className="text-xs text-neutral-dark-medium dark:text-gray-300 hover:text-primary">Редактировать</button>
                      <button type="button" onClick={() => archive(p.id)} className="text-xs text-rose-500 hover:text-rose-600">Архивировать</button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={() => setShowCreate(false)}>
          <div className="bg-white dark:bg-gray-800 rounded-card p-4 w-full max-w-md flex flex-col gap-3" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-bold text-neutral-dark-darkest dark:text-white">{editId ? 'Редактировать проект' : 'Новый проект'}</h2>
            <input type="text" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Название проекта" className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2 text-sm" />
            <input type="text" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="Описание (опц.)" className="w-full border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white px-3 py-2 text-sm" />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setShowCreate(false)} className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">Отмена</button>
              <button type="button" disabled={!form.name.trim() || busy} onClick={submit} className="px-4 py-2 bg-primary text-white rounded hover:bg-primary-dark disabled:opacity-40">{editId ? 'Сохранить' : 'Создать'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 2: tasks/page.tsx — топ-вкладка «Проекты»**

Импорт (рядом с другими component-импортами):
```tsx
import { ProjectsTab } from '../components/ProjectsTab';
```
Добавить состояние верхнего вида рядом с `supportTab` (после строки `const [supportTab, ...]`):
```tsx
  const [topView, setTopView] = useState<'tasks' | 'projects'>('tasks');
```
Добавить верхний таб-бар «Задачи / Проекты», видимый ВСЕМ. Вставить его ПЕРЕД блоком `{isOperator && (...)}` (≈строка 700), чтобы он шёл первым:
```tsx
            <div className="flex gap-2 border-b border-neutral-light-dark dark:border-gray-700 mb-4">
              <button
                onClick={() => setTopView('tasks')}
                className={`px-4 py-2 text-sm font-medium transition-colors ${
                  topView === 'tasks'
                    ? 'border-b-2 border-primary text-primary dark:text-primary-light'
                    : 'text-neutral-dark-medium dark:text-gray-400'
                }`}
              >
                Задачи
              </button>
              <button
                onClick={() => setTopView('projects')}
                className={`px-4 py-2 text-sm font-medium transition-colors ${
                  topView === 'projects'
                    ? 'border-b-2 border-primary text-primary dark:text-primary-light'
                    : 'text-neutral-dark-medium dark:text-gray-400'
                }`}
              >
                Проекты
              </button>
            </div>
            {topView === 'projects' && (
              <ProjectsTab currentUserId={currentUser?.id} isAdmin={currentUser?.isAdmin} />
            )}
```
Обернуть СУЩЕСТВУЮЩИЙ контент задач (операторский таб-бар на ≈700 и обе ветки контента на ≈725 и ≈1131) в условие `topView === 'tasks'`. Минимальный способ: на строке ≈700 заменить начало `{isOperator && (` на `{topView === 'tasks' && isOperator && (`, а две контент-ветки — `{topView === 'tasks' && (!isOperator || supportTab === 'tasks') && (` и `{topView === 'tasks' && isOperator && supportTab === 'support' && (`. (Сохранить существующую JSX внутри без изменений.)

> Прим.: точные строки могли сдвинуться — найти эти три условия по их текущему тексту (`{isOperator && (`, `{(!isOperator || supportTab === 'tasks') && (`, `{isOperator && supportTab === 'support' && (`) и навесить префикс `topView === 'tasks' &&`.

- [ ] **Step 3: Ослабить инлайн-гейт «+» создания проекта (создавать может любой)**

В `tasks/page.tsx` (≈строка 1235) заменить:
```tsx
                {(currentUser?.isAdmin || (currentUser as any)?.isHead) && (
```
на (показывать всем — движок теперь разрешает создание любому):
```tsx
                {true && (
```
(или просто убрать обёртку-условие вокруг кнопки «+». Цель — кнопка видна всем.)

- [ ] **Step 4: Typecheck + build**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit` → exit 0; затем `npm run build` → успешно.

- [ ] **Step 5: Self-review** — компонент один, гейт canModify зеркалит движок, бейдж по `project.departmentId`, инлайн-создание открыто всем.

- [ ] **Step 6: Checkpoint**

---

## Task 9: Ручная e2e-проверка

> На работающей среде (после деплоя движка с миграцией 046 + вебклиента). Аккаунты: обычный юзер, head отдела A, head отдела B, admin.

- [ ] Обычный юзер: вкладка «Проекты» видна; «Создать проект» работает; видит только свои созданные + проекты, где он в задаче; чужие не видит.
- [ ] Создатель: видит «Редактировать»/«Архивировать» на своём; правка имени/описания и архив работают.
- [ ] head A: видит проекты, созданные членами отдела A (даже без личного участия); может их править/архивировать; проект отдела B без его участия — НЕ видит; если втянут в задачу проекта B — видит с бейджем «Другой отдел», но без кнопок правки.
- [ ] admin: видит все проекты орги; правит/архивирует любой.
- [ ] Создание проекта проставляет `department_id` = отдел создателя (проверка: head того же отдела затем видит/правит этот проект).
- [ ] Инлайн «+ создать проект» в форме задачи доступен обычному юзеру и работает.

- [ ] **Checkpoint** — все сценарии зелёные.

---

## Self-review notes

- **Spec coverage:** §3.0 миграция→T1; модель/storage→T2; права→T3; видимость→T4; common→T5; NestJS→T6; useProjects→T7; вкладка+бейдж+гейт+инлайн-гейт→T8; тесты→T2-T4+T9. `engine_service.py` НЕ меняется (ProjectService без новых deps) — соответствует спеке §6.
- **Permissions:** create — любой (T3 убирает `_check_can_create`); modify — `_check_can_modify` (creator/admin/head-same-dept). Frontend `canModify` зеркалит (T8), движок энфорсит.
- **Frozen department:** `update` SQL не трогает `department_id` (T2 не меняет update) — заморозка соблюдена.
- **departmentId через слои:** engine to_dict (T2) → NestJS mapProject (T6) → common type (T5) → useProjects local type (T7) → ProjectsTab (T8). Имя поля консистентно: snake `department_id` в движке, camel `departmentId` дальше.
- **Frontend без jest** — T5-T8 проверяются tsc/build + ручной e2e (T9).
- Если `tasks/page.tsx`-условия сдвинулись по строкам — искать по тексту условий (Step 2 T8 это оговаривает).
