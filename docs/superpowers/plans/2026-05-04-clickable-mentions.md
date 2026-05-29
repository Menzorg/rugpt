# Кликабельные упоминания — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать `@user`/`@@user` в сообщениях кликабельными — popup с инфой о пользователе. Один компонент `MentionLink` + Provider с lifted-popup-state и кэшем + remark-плагин для интеграции с markdown.

**Architecture:** Новый модуль `app/components/mentions/`: `MentionsProvider` (Context + один global popup + Map-кэш загруженных user'ов), `MentionLink` (тонкая кнопка-цель клика), `MentionPopup` (адаптивный — bottom-sheet на mobile, anchored tooltip на desktop), `remarkMentions` (плагин для react-markdown). Engine добавляет одно поле `department_name` в `UserResponse`. В чат-страницах оборачиваем content в Provider.

**Tech Stack:** Next.js 15, React 19, react-markdown 9.x, unist-util-visit (transitive), TailwindCSS, FastAPI, asyncpg.

**Spec:** `/root/rugpt/docs/superpowers/specs/2026-05-04-clickable-mentions-design.md`

**No commits:** пользователь коммитит сам. В шагах нет `git commit`.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/engine/models/user.py` | Modify | Добавить опциональное `department_name` |
| `src/engine/storage/user_storage.py` | Modify | `get_by_username` — JOIN с departments, заполнять department_name |
| `src/engine/routes/users.py` | Modify | `UserResponse` отдаёт `department_name` |
| `tests/test_users_routes.py` (или новый) | Modify/Create | Тест что endpoint возвращает department_name |
| `packages/frontend/src/app/components/mentions/types.ts` | Create | `UserData`, `MentionKind`, `ActivePopup` |
| `packages/frontend/src/app/components/mentions/MentionsProvider.tsx` | Create | Context + state + fetch + рендер MentionPopup |
| `packages/frontend/src/app/components/mentions/MentionLink.tsx` | Create | Кнопка-target клика |
| `packages/frontend/src/app/components/mentions/MentionPopup.tsx` | Create | UI popup'а, adaptive layout, auto-flip |
| `packages/frontend/src/app/components/mentions/remarkMentions.ts` | Create | Remark-плагин |
| `packages/frontend/src/app/components/mentions/MentionRenderer.tsx` | Create | Мост: AST-нода `mention` → `<MentionLink>` |
| `packages/frontend/src/app/components/mentions/parseMentionsToNodes.tsx` | Create | Helper для refs-ветки MessageBubble |
| `packages/frontend/src/app/components/mentions/index.ts` | Create | Barrel export |
| `packages/frontend/src/app/components/MessageBubble.tsx` | Modify | Удалить `wireToDisplay`; markdown-ветка использует remarkMentions; refs-ветка — parseMentionsToNodes |
| `packages/frontend/src/app/components/MainChat.tsx` | Modify | Обернуть в `<MentionsProvider>` |
| `packages/frontend/src/app/chat/[username]/page.tsx` | Modify | Обернуть в `<MentionsProvider>` |
| `packages/frontend/src/app/chat/task/[id]/page.tsx` | Modify | Обернуть в `<MentionsProvider>` |
| `packages/frontend/src/app/chat/project/[id]/page.tsx` | Modify | Обернуть в `<MentionsProvider>` |
| `packages/frontend/src/app/chat/support/[id]/page.tsx` | Modify | Обернуть в `<MentionsProvider>` |

---

### Task 1: Engine — добавить `department_name` в User model

**Files:**
- Modify: `/root/rugpt/src/engine/models/user.py`

- [ ] **Step 1: Прочесть текущий User**

Run: `grep -n "department_id\|department_name\|class User\|@dataclass" /root/rugpt/src/engine/models/user.py`

Должно показать `department_id: Optional[UUID] = None` поле в @dataclass User.

- [ ] **Step 2: Добавить поле**

В `/root/rugpt/src/engine/models/user.py` найти строку `department_id: Optional[UUID] = None` (внутри `@dataclass class User`). Добавить после неё:

```python
    department_name: Optional[str] = None
```

- [ ] **Step 3: Если есть `to_dict` метод — добавить туда тоже**

Run: `grep -n "to_dict\|department_id" /root/rugpt/src/engine/models/user.py`

Если `to_dict` метод есть и сериализует `department_id` — добавить рядом:

```python
            "department_name": self.department_name,
```

(Не меняем `from_dict` если он есть — поле опциональное, дефолтится в None.)

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/models/user.py').read()); print('parse ok')"`

Expected: `parse ok`

---

### Task 2: Engine — `UserStorage.get_by_username` JOIN с departments

**Files:**
- Modify: `/root/rugpt/src/engine/storage/user_storage.py`

- [ ] **Step 1: Найти текущий get_by_username**

Run: `grep -n "get_by_username\|FROM users" /root/rugpt/src/engine/storage/user_storage.py`

Это даст линию объявления метода и SQL.

- [ ] **Step 2: Прочесть метод**

Прочитать метод `get_by_username` и понять текущий SQL и mapping в User объект.

- [ ] **Step 3: Расширить SQL — LEFT JOIN на departments**

Заменить `SELECT u.* FROM users u WHERE u.username = $1` (или похожий) на:

```sql
SELECT u.*, d.name AS department_name
FROM users u
LEFT JOIN departments d ON d.id = u.department_id
WHERE u.username = $1
```

(Точный текст зависит от текущего SQL — сохрани остальные условия типа `is_active=true` если они есть.)

- [ ] **Step 4: Заполнить department_name в результат**

Где собирается User объект из row — добавить `department_name=row.get('department_name')` (или соответствующий paramater способ — зависит от того, как остальные поля передаются).

Пример (если конструируется так):
```python
return User(
    id=row['id'],
    ...
    department_id=row['department_id'],
    department_name=row.get('department_name'),
)
```

- [ ] **Step 5: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/storage/user_storage.py').read()); print('parse ok')"`

Expected: `parse ok`

- [ ] **Step 6: Smoke-тест прямо из БД**

Run:
```
/root/rugpt/venv/bin/python -c "
import asyncio
from src.engine.services.engine_service import EngineService
async def go():
    e = EngineService()
    await e.initialize()
    user = await e.user_storage.get_by_username('chu')  # подставь реального юзера
    print('user:', user)
    print('department_name:', getattr(user, 'department_name', '<missing>'))
    await e.close()
asyncio.run(go())
"
```

Expected: атрибут `department_name` присутствует (значение зависит от данных).

---

### Task 3: Engine — `UserResponse` отдаёт `department_name`

**Files:**
- Modify: `/root/rugpt/src/engine/routes/users.py`

- [ ] **Step 1: Найти UserResponse класс**

Run: `grep -n "class UserResponse\|department_id" /root/rugpt/src/engine/routes/users.py`

- [ ] **Step 2: Добавить поле в UserResponse**

В `/root/rugpt/src/engine/routes/users.py` найти класс `UserResponse(BaseModel)`. После строки `department_id: Optional[str] = None` добавить:

```python
    department_name: Optional[str] = None
```

- [ ] **Step 3: Найти endpoint `/username/{username}` и проверить что UserResponse строится из User модели**

Run: `grep -nA 8 "username/{username}" /root/rugpt/src/engine/routes/users.py`

Если endpoint возвращает `UserResponse(**user.__dict__)` или `UserResponse.model_validate(user)` — поле подхватится автоматически.

Если есть ручной маппинг полей — добавить:
```python
        department_name=user.department_name,
```

- [ ] **Step 4: Sanity-парсер**

Run: `/root/rugpt/venv/bin/python -c "import ast; ast.parse(open('/root/rugpt/src/engine/routes/users.py').read()); print('parse ok')"`

Expected: `parse ok`

- [ ] **Step 5: Live-test через curl (после рестарта engine на dev)**

После рестарта engine'а:
```
curl -s http://127.0.0.1:8100/api/v1/users/username/chu -H "Authorization: Bearer <token>" | python3 -c "import json,sys; d=json.load(sys.stdin); print('department_name:', d.get('department_name'))"
```

Expected: `department_name: <строка или None>`

(На dev-машине engine может не крутиться — тогда просто полагаемся на парсер + smoke из Task 2.)

---

### Task 4: Engine — тест на `get_user_by_username` отдаёт `department_name`

**Files:**
- Create or modify: `/root/rugpt/tests/test_users_routes.py` (если файла нет — создать; если есть — добавить тест)

- [ ] **Step 1: Проверить существование файла**

Run: `ls /root/rugpt/tests/test_users_routes.py 2>/dev/null && echo exists || echo missing`

- [ ] **Step 2: Если existing — добавить тест в конец, иначе создать файл**

Если **existing**, добавить в конец файла:

```python
import pytest
import os
import asyncpg
from uuid import uuid4
from src.engine.services.engine_service import EngineService

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


@pytest.mark.asyncio
async def test_get_by_username_includes_department_name():
    """get_by_username должен возвращать department_name (через JOIN с departments)."""
    engine = EngineService()
    await engine.initialize()
    pool = await asyncpg.create_pool(DSN)
    org_id = None
    user_id = None
    dept_id = None
    try:
        async with pool.acquire() as conn:
            org_id = await conn.fetchval(
                "INSERT INTO organizations (id, name, slug) "
                "VALUES (gen_random_uuid(), 'dt', $1) RETURNING id",
                f"dt_{uuid4().hex[:8]}",
            )
            dept_id = await conn.fetchval(
                "INSERT INTO departments (id, org_id, name) "
                "VALUES (gen_random_uuid(), $1, $2) RETURNING id",
                org_id, "Юристы",
            )
            uname = f"dt_user_{uuid4().hex[:8]}"
            user_id = await conn.fetchval(
                "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active, department_id) "
                "VALUES (gen_random_uuid(), $1, $2, $2, 'x', $3, true, $4) RETURNING id",
                org_id, uname, f"{uname}@test.local", dept_id,
            )

        user = await engine.user_storage.get_by_username(uname)
        assert user is not None
        assert user.department_id == dept_id
        assert user.department_name == "Юристы"
    finally:
        async with pool.acquire() as conn:
            if user_id:
                await conn.execute("DELETE FROM users WHERE id = $1", user_id)
            if dept_id:
                await conn.execute("DELETE FROM departments WHERE id = $1", dept_id)
            if org_id:
                await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
        await pool.close()
        await engine.close()
```

Если файла **нет** — создать с тем же содержимым плюс docstring сверху.

- [ ] **Step 3: Запустить тест**

Run: `cd /root/rugpt && venv/bin/pytest tests/test_users_routes.py::test_get_by_username_includes_department_name -v 2>&1 | tail -15`

Expected: `1 passed`. Если БД недоступна — skip; запиши какое.

---

### Task 5: Frontend — создать `mentions/types.ts`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/types.ts`

- [ ] **Step 1: Создать файл**

Создать `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/types.ts` с содержимым:

```typescript
/**
 * Типы для модуля mentions: данные пользователя для popup'а, тип упоминания,
 * активный popup-state.
 */

export type MentionKind = 'user' | 'role';

export interface UserData {
  id: string;
  username: string;
  name: string;
  email: string;
  roleId: string | null;
  roleName: string | null;
  departmentId: string | null;
  departmentName: string | null;
  isAdmin: boolean;
  isHead: boolean;
}

export interface MentionError {
  error: string;
}

export type CacheEntry = UserData | MentionError;

export function isMentionError(v: CacheEntry): v is MentionError {
  return (v as MentionError).error !== undefined;
}

export interface ActivePopup {
  username: string;
  kind: MentionKind;
  /** Bounding rect of the click target — для позиционирования tooltip'а (desktop). */
  anchor: DOMRect;
}
```

- [ ] **Step 2: Проверить TS**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "mentions/types" | head -5 || echo clean`

Expected: `clean` (без ошибок).

---

### Task 6: Frontend — создать `mentions/MentionsProvider.tsx`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/MentionsProvider.tsx`

- [ ] **Step 1: Создать файл с провайдером**

Создать `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/MentionsProvider.tsx`:

```typescript
'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { getApiClient } from '../../../transport/apiClient';
import { useAuthStore } from '../../hooks/useAuth';
import { MentionPopup } from './MentionPopup';
import type { ActivePopup, CacheEntry, MentionKind, UserData } from './types';

interface MentionsContextValue {
  active: ActivePopup | null;
  cache: Map<string, CacheEntry>;
  loading: Set<string>;
  openMention: (username: string, kind: MentionKind, anchor: DOMRect) => void;
  closeMention: () => void;
}

const MentionsContext = createContext<MentionsContextValue | null>(null);

export function useMentions(): MentionsContextValue {
  const ctx = useContext(MentionsContext);
  if (!ctx) {
    throw new Error('useMentions must be used inside <MentionsProvider>');
  }
  return ctx;
}

interface MentionsProviderProps {
  children: ReactNode;
}

/**
 * Провайдер кликабельных упоминаний. Хранит:
 * - один активный popup (нельзя открыть два сразу — UX).
 * - Map-кэш загруженных user'ов (повторный клик на тот же @anna не делает fetch).
 *
 * Рендерит MentionPopup поверх children (через portal делать не стали —
 * popup уже fixed-positioned, z-50 хватает).
 */
export function MentionsProvider({ children }: MentionsProviderProps) {
  const [active, setActive] = useState<ActivePopup | null>(null);
  const [cache, setCache] = useState<Map<string, CacheEntry>>(new Map());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const currentUserId = useAuthStore((s) => s.user?.id);

  const fetchUser = useCallback(async (username: string) => {
    setLoading((prev) => {
      const next = new Set(prev);
      next.add(username);
      return next;
    });
    try {
      const api = getApiClient();
      const raw = await api.signedGet<any>(`/api/users/username/${encodeURIComponent(username)}`, currentUserId);
      const data: UserData = {
        id: raw.id,
        username: raw.username,
        name: raw.name,
        email: raw.email,
        roleId: raw.role_id ?? raw.roleId ?? null,
        roleName: raw.role_name ?? raw.roleName ?? null,
        departmentId: raw.department_id ?? raw.departmentId ?? null,
        departmentName: raw.department_name ?? raw.departmentName ?? null,
        isAdmin: Boolean(raw.is_admin ?? raw.isAdmin),
        isHead: Boolean(raw.is_head ?? raw.isHead),
      };
      setCache((prev) => new Map(prev).set(username, data));
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Не удалось загрузить инфо';
      setCache((prev) => new Map(prev).set(username, { error: msg }));
    } finally {
      setLoading((prev) => {
        const next = new Set(prev);
        next.delete(username);
        return next;
      });
    }
  }, [currentUserId]);

  const openMention = useCallback((username: string, kind: MentionKind, anchor: DOMRect) => {
    setActive({ username, kind, anchor });
    if (!cache.has(username) && !loading.has(username)) {
      void fetchUser(username);
    }
  }, [cache, loading, fetchUser]);

  const closeMention = useCallback(() => setActive(null), []);

  // Закрытие на scroll: anchor устаревает, а пересчитывать каждый кадр дорого.
  useEffect(() => {
    if (!active) return;
    const onScroll = () => closeMention();
    window.addEventListener('scroll', onScroll, true);
    return () => window.removeEventListener('scroll', onScroll, true);
  }, [active, closeMention]);

  const value = useMemo<MentionsContextValue>(() => ({
    active, cache, loading, openMention, closeMention,
  }), [active, cache, loading, openMention, closeMention]);

  return (
    <MentionsContext.Provider value={value}>
      {children}
      {active && <MentionPopup />}
    </MentionsContext.Provider>
  );
}
```

- [ ] **Step 2: Проверить TS (поломается — MentionPopup ещё нет)**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "MentionsProvider|MentionPopup" | head -5`

Ожидание: ошибка про отсутствие `./MentionPopup`. Это OK — фиксим в Task 7.

---

### Task 7: Frontend — создать `mentions/MentionPopup.tsx`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/MentionPopup.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMentions } from './MentionsProvider';
import { isMentionError, type CacheEntry, type UserData } from './types';

const POPUP_ESTIMATED_HEIGHT = 260;
const POPUP_DESKTOP_WIDTH = 280;
const VIEWPORT_GUTTER = 8;

/**
 * Popup для упоминания пользователя.
 *
 * Mobile (<sm) — bottom-sheet: затемнённый backdrop + панель снизу на всю ширину viewport.
 * Desktop (sm+) — fixed-positioned tooltip у клика, auto-flip вверх если внизу мало места,
 * clamp по горизонтали чтобы не вылезть за края.
 */
export function MentionPopup() {
  const { active, cache, loading, closeMention } = useMentions();

  // Закрытие по Esc.
  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeMention();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [active, closeMention]);

  if (!active) return null;
  const entry = cache.get(active.username);
  const isLoading = loading.has(active.username) && entry === undefined;

  return (
    <>
      {/* Mobile backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/30 sm:hidden"
        onClick={closeMention}
      />
      {/* Desktop click-outside detector — без затемнения */}
      <div
        className="hidden sm:block fixed inset-0 z-40"
        onClick={closeMention}
      />
      <PopupCard
        active={active}
        entry={entry}
        loading={isLoading}
      />
    </>
  );
}

interface PopupCardProps {
  active: NonNullable<ReturnType<typeof useMentions>['active']>;
  entry: CacheEntry | undefined;
  loading: boolean;
}

function PopupCard({ active, entry, loading }: PopupCardProps) {
  const desktopStyle = useMemo(() => computeDesktopPosition(active.anchor), [active.anchor]);

  return (
    <div
      onClick={(e) => e.stopPropagation()}
      style={desktopStyle}
      className="
        fixed inset-x-2 bottom-2 z-50 max-h-[80dvh] overflow-y-auto
        rounded-card bg-white dark:bg-gray-800 shadow-2xl
        border border-neutral-light-dark dark:border-gray-600 p-4
        sm:inset-x-auto sm:bottom-auto sm:max-h-none sm:w-[280px] sm:p-3
      "
    >
      {active.kind === 'role' && (
        <div className="mb-2 text-[11px] font-bold uppercase tracking-wide text-primary dark:text-primary-light">
          Роль пользователя
        </div>
      )}
      {loading && (
        <div className="text-sm text-neutral-dark-medium dark:text-gray-400">Загрузка…</div>
      )}
      {entry && isMentionError(entry) && (
        <div className="text-sm text-neutral-dark-medium dark:text-gray-400">
          {entry.error.includes('404') || entry.error.toLowerCase().includes('not found')
            ? 'Пользователь не найден'
            : `Не удалось загрузить инфо: ${entry.error}`}
        </div>
      )}
      {entry && !isMentionError(entry) && <UserCard data={entry} />}
    </div>
  );
}

function UserCard({ data }: { data: UserData }) {
  return (
    <div className="space-y-1">
      <div className="text-base font-bold text-neutral-dark-darkest dark:text-white">
        {data.name}
      </div>
      <div className="text-sm font-mono text-neutral-dark-medium dark:text-gray-400">
        @{data.username}
      </div>
      {data.email && (
        <div className="text-sm text-neutral-dark-medium dark:text-gray-300">{data.email}</div>
      )}
      {data.roleName && (
        <div className="text-sm text-neutral-dark-medium dark:text-gray-300">
          Роль: <span className="text-neutral-dark-darkest dark:text-gray-100">{data.roleName}</span>
        </div>
      )}
      {data.departmentName && (
        <div className="text-sm text-neutral-dark-medium dark:text-gray-300">
          Отдел: <span className="text-neutral-dark-darkest dark:text-gray-100">{data.departmentName}</span>
        </div>
      )}
    </div>
  );
}

function computeDesktopPosition(anchor: DOMRect): React.CSSProperties {
  // Auto-flip: если внизу < POPUP_ESTIMATED_HEIGHT — open вверх, иначе вниз.
  const spaceBelow = window.innerHeight - anchor.bottom;
  const flipUp = spaceBelow < POPUP_ESTIMATED_HEIGHT && anchor.top > POPUP_ESTIMATED_HEIGHT;
  const top = flipUp ? Math.max(VIEWPORT_GUTTER, anchor.top - POPUP_ESTIMATED_HEIGHT) : anchor.bottom + 4;
  const rawLeft = anchor.left;
  const left = Math.min(
    Math.max(VIEWPORT_GUTTER, rawLeft),
    window.innerWidth - POPUP_DESKTOP_WIDTH - VIEWPORT_GUTTER,
  );
  return { top, left };
}
```

- [ ] **Step 2: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "MentionPopup|MentionsProvider" | head -10`

Expected: `clean` (или не упоминающие mentions/* ошибки).

---

### Task 8: Frontend — создать `mentions/MentionLink.tsx`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/MentionLink.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { ReactNode } from 'react';
import { useMentions } from './MentionsProvider';
import type { MentionKind } from './types';

interface MentionLinkProps {
  username: string;
  kind?: MentionKind;
  children: ReactNode;
}

/**
 * Тонкая кнопка-цель клика для упоминания пользователя.
 * Сама ничего не фетчит — отдаёт click в MentionsProvider, тот открывает popup.
 *
 * Generic: caller сам решает что показать на кнопке (children) — эмоджи + username,
 * полное имя, или что угодно. В чате — `🧑username` / `🤖username`. В /tasks может
 * быть просто `Анна Юрьевна`.
 */
export function MentionLink({ username, kind = 'user', children }: MentionLinkProps) {
  const { openMention } = useMentions();
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        openMention(username, kind, e.currentTarget.getBoundingClientRect());
      }}
      className="text-primary dark:text-primary-light hover:underline cursor-pointer bg-transparent border-0 p-0 font-inherit"
    >
      {children}
    </button>
  );
}
```

- [ ] **Step 2: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "mentions/" | head -10 || echo clean`

Expected: `clean`.

---

### Task 9: Frontend — создать `mentions/remarkMentions.ts` (плагин)

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/remarkMentions.ts`

- [ ] **Step 1: Проверить наличие unist-util-visit**

Run: `cd /root/webclient_rugpt/packages/frontend && node -e "console.log(require.resolve('unist-util-visit'))" 2>&1 | head -3`

Если резолвится — OK. Если нет — пропустить шаг 1 и установить:

Run: `cd /root/webclient_rugpt/packages/frontend && npm install unist-util-visit`

- [ ] **Step 2: Создать плагин**

Создать `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/remarkMentions.ts`:

```typescript
/**
 * Remark-плагин: парсит `@user` и `@@user` в текстовых нодах markdown AST,
 * заменяет их на кастомные ноды type='mention' с {kind, username}.
 *
 * react-markdown через `components.mention` отрендерит эти ноды как
 * <MentionLink>. Не нужно encode'ить mentions в markdown-link href и
 * перехватывать в components.a — плагин делает всё на AST-уровне.
 */
import type { Plugin } from 'unified';
import type { Root } from 'mdast';
import { visit, SKIP } from 'unist-util-visit';

const MENTION_RE = /(^|\s)(@@?)(\w+)/g;

export const remarkMentions: Plugin<[], Root> = () => (tree) => {
  visit(tree, 'text', (node: any, index, parent: any) => {
    if (!parent || index == null || typeof node.value !== 'string') return;
    const text: string = node.value;
    MENTION_RE.lastIndex = 0;
    const matches = [...text.matchAll(MENTION_RE)];
    if (matches.length === 0) return;

    const newNodes: any[] = [];
    let cursor = 0;
    for (const m of matches) {
      const [full, prefix, at, username] = m;
      const matchIdx = m.index ?? 0;
      const start = matchIdx + prefix.length;
      if (start > cursor) {
        newNodes.push({ type: 'text', value: text.slice(cursor, start) });
      }
      newNodes.push({
        type: 'mention',
        kind: at === '@@' ? 'role' : 'user',
        username,
        // hName сообщает react-markdown'у что в hast эту ноду рендерить
        // как HTML-tag <mention>; этот тег override'ится через components.mention.
        data: { hName: 'mention', hProperties: { username, kind: at === '@@' ? 'role' : 'user' } },
      });
      cursor = matchIdx + full.length;
    }
    if (cursor < text.length) {
      newNodes.push({ type: 'text', value: text.slice(cursor) });
    }
    parent.children.splice(index, 1, ...newNodes);
    return [SKIP, index + newNodes.length];
  });
};
```

- [ ] **Step 3: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "remarkMentions|mentions/" | head -10 || echo clean`

Expected: `clean`.

---

### Task 10: Frontend — создать `mentions/MentionRenderer.tsx` (мост AST → React)

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/MentionRenderer.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import { MentionLink } from './MentionLink';
import type { MentionKind } from './types';

const ROBOT_EMOJI = '🤖';
const PERSON_EMOJI = '🧑';

interface MentionRendererProps {
  username?: string;
  kind?: MentionKind;
}

/**
 * Мост между AST-нодой `mention` (создана плагином remarkMentions) и React.
 * react-markdown передаёт `hProperties` плагина (username, kind) как props
 * сюда через components.mention.
 *
 * Эмоджи 🧑/🤖 живёт здесь — внутри children для MentionLink. MentionLink
 * сам остаётся generic (без знания об эмоджи).
 */
export function MentionRenderer({ username, kind }: MentionRendererProps) {
  if (!username) return null;
  const k: MentionKind = kind === 'role' ? 'role' : 'user';
  const emoji = k === 'role' ? ROBOT_EMOJI : PERSON_EMOJI;
  return (
    <MentionLink username={username} kind={k}>
      {emoji}{username}
    </MentionLink>
  );
}
```

- [ ] **Step 2: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "MentionRenderer|mentions/" | head -10 || echo clean`

Expected: `clean`.

---

### Task 11: Frontend — создать `mentions/parseMentionsToNodes.tsx` (helper для refs-ветки)

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/parseMentionsToNodes.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { ReactNode } from 'react';
import { MentionLink } from './MentionLink';

const ROBOT_EMOJI = '🤖';
const PERSON_EMOJI = '🧑';
const MENTION_RE = /(^|\s)(@@?)(\w+)/g;

/**
 * Разбивает plain-text строку на массив ReactNode: текстовые куски + <MentionLink>
 * на местах @user / @@user. Используется в refs-ветке MessageContent (где markdown
 * выключен и нельзя прогнать через remark-плагин).
 *
 * Эмоджи 🧑/🤖 встраиваются в children MentionLink — параллельно с MentionRenderer.
 */
export function parseMentionsToNodes(text: string, keyPrefix = 'm'): ReactNode[] {
  MENTION_RE.lastIndex = 0;
  const matches = [...text.matchAll(MENTION_RE)];
  if (matches.length === 0) return [text];

  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const m of matches) {
    const [full, prefix, at, username] = m;
    const matchIdx = m.index ?? 0;
    const start = matchIdx + prefix.length;
    if (start > cursor) nodes.push(text.slice(cursor, start));
    const kind = at === '@@' ? 'role' : 'user';
    const emoji = kind === 'role' ? ROBOT_EMOJI : PERSON_EMOJI;
    nodes.push(
      <MentionLink key={`${keyPrefix}-${matchIdx}`} username={username} kind={kind}>
        {emoji}{username}
      </MentionLink>
    );
    cursor = matchIdx + full.length;
  }
  if (cursor < text.length) nodes.push(text.slice(cursor));
  return nodes;
}
```

- [ ] **Step 2: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "parseMentionsToNodes|mentions/" | head -10 || echo clean`

Expected: `clean`.

---

### Task 12: Frontend — barrel `mentions/index.ts`

**Files:**
- Create: `/root/webclient_rugpt/packages/frontend/src/app/components/mentions/index.ts`

- [ ] **Step 1: Создать файл**

```typescript
export { MentionsProvider, useMentions } from './MentionsProvider';
export { MentionLink } from './MentionLink';
export { MentionRenderer } from './MentionRenderer';
export { remarkMentions } from './remarkMentions';
export { parseMentionsToNodes } from './parseMentionsToNodes';
export type { MentionKind, UserData, ActivePopup } from './types';
```

- [ ] **Step 2: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "mentions/index" | head -5 || echo clean`

Expected: `clean`.

---

### Task 13: Frontend — интеграция в `MessageBubble.tsx`

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/components/MessageBubble.tsx`

- [ ] **Step 1: Прочесть текущее состояние wireToDisplay/MessageContent**

Run: `grep -n "wireToDisplay\|MessageContent\|renderContentWithReferences\|ReactMarkdown\|ROBOT_EMOJI\|PERSON_EMOJI" /root/webclient_rugpt/packages/frontend/src/app/components/MessageBubble.tsx | head -20`

- [ ] **Step 2: Заменить wireToDisplay + MessageContent + renderContentWithReferences**

В `/root/webclient_rugpt/packages/frontend/src/app/components/MessageBubble.tsx` найти текущий блок:

```tsx
// Display-side: @user → 🧑user, @@role → 🤖role. Парный с textToWire в ChatInput
// (там обратное преобразование при отправке). @@ заменяется первым (жадно),
// иначе `@@anna` распадётся на два `@` после single-pattern.
const ROBOT_EMOJI = '🤖';
const PERSON_EMOJI = '🧑';
function wireToDisplay(text: string): string {
  return text
    .replace(/(^|\s)@@(\w+)/g, `$1${ROBOT_EMOJI}$2`)
    .replace(/(^|\s)@(\w+)/g, `$1${PERSON_EMOJI}$2`);
}

function renderContentWithReferences(content: string, references?: MessageReference[]): ReactNode[] {
  if (!references || references.length === 0) return [wireToDisplay(content)];
  const sorted = [...references].sort((a, b) => a.position - b.position);
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const ref of sorted) {
    const tokenLen = (ref.type === 'task' ? 1 : 2) + 36;
    if (ref.position > cursor) out.push(wireToDisplay(content.slice(cursor, ref.position)));
    out.push(<ReferencePill key={`${ref.type}-${ref.position}`} ref={ref} />);
    cursor = ref.position + tokenLen;
  }
  if (cursor < content.length) out.push(wireToDisplay(content.slice(cursor)));
  return out;
}

function MessageContent({ text, references }: { text: string; references?: MessageReference[] }) {
  // If there are references, bypass markdown for a plain-text render so pills
  // can be inlined safely. Existing markdown support stays for refless messages.
  if (references && references.length > 0) {
    return (
      <div className="text-body-md leading-relaxed break-words whitespace-pre-wrap">
        {renderContentWithReferences(text, references)}
      </div>
    );
  }
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>{wireToDisplay(text)}</ReactMarkdown>
  );
}
```

Заменить на:

```tsx
function renderContentWithReferences(content: string, references?: MessageReference[]): ReactNode[] {
  if (!references || references.length === 0) return parseMentionsToNodes(content);
  const sorted = [...references].sort((a, b) => a.position - b.position);
  const out: ReactNode[] = [];
  let cursor = 0;
  for (const ref of sorted) {
    const tokenLen = (ref.type === 'task' ? 1 : 2) + 36;
    if (ref.position > cursor) {
      out.push(...parseMentionsToNodes(content.slice(cursor, ref.position), `r-${cursor}`));
    }
    out.push(<ReferencePill key={`${ref.type}-${ref.position}`} ref={ref} />);
    cursor = ref.position + tokenLen;
  }
  if (cursor < content.length) {
    out.push(...parseMentionsToNodes(content.slice(cursor), `r-${cursor}`));
  }
  return out;
}

function MessageContent({ text, references }: { text: string; references?: MessageReference[] }) {
  // Если есть references — bypass markdown (pills нужно встраивать как ноды).
  // Mentions в этой ветке обрабатывает parseMentionsToNodes.
  if (references && references.length > 0) {
    return (
      <div className="text-body-md leading-relaxed break-words whitespace-pre-wrap">
        {renderContentWithReferences(text, references)}
      </div>
    );
  }
  // Markdown-ветка: remarkMentions делает mentions на AST-уровне,
  // components.mention рендерит их через MentionRenderer.
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMentions]}
      components={{ mention: MentionRenderer as any }}
    >
      {text}
    </ReactMarkdown>
  );
}
```

- [ ] **Step 3: Добавить импорты**

В `/root/webclient_rugpt/packages/frontend/src/app/components/MessageBubble.tsx` найти секцию импортов (топ файла, строки ~1-15). Добавить:

```typescript
import { remarkMentions } from './mentions/remarkMentions';
import { MentionRenderer } from './mentions/MentionRenderer';
import { parseMentionsToNodes } from './mentions/parseMentionsToNodes';
```

- [ ] **Step 4: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | grep -E "MessageBubble|mentions/" | head -15 || echo clean`

Expected: `clean`.

- [ ] **Step 5: Verify wireToDisplay полностью удалён**

Run: `grep -n "wireToDisplay\|ROBOT_EMOJI\|PERSON_EMOJI" /root/webclient_rugpt/packages/frontend/src/app/components/MessageBubble.tsx || echo clean`

Expected: `clean` (всё удалено вместе с заменённым блоком).

---

### Task 14: Frontend — обернуть chat-страницы в `<MentionsProvider>`

**Files:**
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/components/MainChat.tsx`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/chat/[username]/page.tsx`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/chat/task/[id]/page.tsx`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/chat/project/[id]/page.tsx`
- Modify: `/root/webclient_rugpt/packages/frontend/src/app/chat/support/[id]/page.tsx`

Один и тот же паттерн в каждом — обернуть **корневой return** в `<MentionsProvider>`.

- [ ] **Step 1: MainChat.tsx**

В `/root/webclient_rugpt/packages/frontend/src/app/components/MainChat.tsx`:

Найти импорт-блок наверху и добавить:
```typescript
import { MentionsProvider } from './mentions';
```

Найти главный `return (...)` (около строки 158, начинается с `<div className="h-dvh ...">`) и обернуть **всё содержимое return'а** в `<MentionsProvider>`:

old:
```tsx
return (
    <div className="h-dvh overflow-x-hidden bg-neutral-light-lightest dark:bg-gray-900 transition-colors pl-[70px]">
```

new:
```tsx
return (
    <MentionsProvider>
    <div className="h-dvh overflow-x-hidden bg-neutral-light-lightest dark:bg-gray-900 transition-colors pl-[70px]">
```

И в самом конце `return` (там где закрывающий `</div>`) добавить закрывающий `</MentionsProvider>`. Найти эту последнюю `</div>` (см. перед `);` в конце функции) — после неё закрывающий `</MentionsProvider>`.

- [ ] **Step 2: chat/[username]/page.tsx**

В `/root/webclient_rugpt/packages/frontend/src/app/chat/[username]/page.tsx` — то же:

Импорт:
```typescript
import { MentionsProvider } from '../../components/mentions';
```

Обернуть основной `return` (находится около `<div className="h-dvh ... pl-[70px]">` ~строка 110) аналогично — `<MentionsProvider>` сразу после `return (` и `</MentionsProvider>` перед `);`.

- [ ] **Step 3: chat/task/[id]/page.tsx**

Тот же паттерн. Импорт:
```typescript
import { MentionsProvider } from '../../../components/mentions';
```

Обернуть основной return (`<div className="flex h-dvh overflow-x-hidden">` ~строка 126).

- [ ] **Step 4: chat/project/[id]/page.tsx**

Импорт:
```typescript
import { MentionsProvider } from '../../../components/mentions';
```

Обернуть основной return (`<div className="flex h-dvh overflow-x-hidden">` ~строка 89).

- [ ] **Step 5: chat/support/[id]/page.tsx**

Импорт:
```typescript
import { MentionsProvider } from '../../../components/mentions';
```

Обернуть основной return (`<div className="flex h-dvh overflow-x-hidden">` ~строка 300).

- [ ] **Step 6: TS-чек**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit 2>&1 | head -10 || echo clean`

Expected: `clean`.

---

### Task 15: Manual E2E

**Files:** none — проверка глазами

После деплоя webclient (frontend + backend) и engine. Проверить в браузере:

- [ ] **Сценарий 1:** в чате отправь сообщение `@anna привет` → пузырь у адресата показывает `🧑anna` как кнопку (подсвечена primary-цветом / hover-underline).

- [ ] **Сценарий 2:** клик на `🧑anna` → popup появился, через мгновение показал имя/email/роль/отдел.

- [ ] **Сценарий 3:** отправь `@@anna эй` → `🤖anna` кликабельно. Клик → popup с жирным header'ом «Роль пользователя» сверху, дальше те же поля.

- [ ] **Сценарий 4:** в одном сообщении два разных mention'а (`@bob @@anna ...`) → клик на первый открывает popup. Клик на второй — первый закрылся, второй открылся.

- [ ] **Сценарий 5:** один и тот же `@anna` упомянут трижды в чате. Открой DevTools Network. Клик на первый → 1 запрос на `/api/users/username/anna`. Клик на второй и третий — 0 новых запросов (взято из cache).

- [ ] **Сценарий 6:** на мобильном вьюпорте (DevTools 320px) → popup рендерится как bottom-sheet (на всю ширину снизу с backdrop'ом).

- [ ] **Сценарий 7:** на десктопе кликни mention находящийся у самого нижнего края экрана → popup открывается **сверху** от кнопки (auto-flip).

- [ ] **Сценарий 8:** упомяни несуществующего юзера `@nosuchuser` (предварительно отключи autocomplete-подсказки или просто наберись в textarea руками). Клик → «Пользователь не найден».

- [ ] **Сценарий 9:** AI-ответ в формате markdown с **жирным**, списком, и `@anna` посередине → markdown-форматирование сохранено, mention кликабелен.

- [ ] **Сценарий 10:** сообщение содержит и references (`!task-uuid`), и `@anna` → и task-pill, и mention рендерятся, оба кликабельны.

- [ ] **Сценарий 11:** popup открыт → нажми Esc → закрылся. Клик вне popup'а → закрылся. Скролл чата → закрылся.

---

## Self-Review

**Spec coverage:**
- ✅ MentionLink (тонкая кнопка, generic) — Task 8.
- ✅ MentionsProvider (один popup, кэш, openMention/closeMention) — Task 6.
- ✅ MentionPopup (bottom-sheet mobile, anchored desktop, auto-flip) — Task 7.
- ✅ remarkMentions плагин — Task 9.
- ✅ MentionRenderer (мост AST→React, эмоджи здесь) — Task 10.
- ✅ parseMentionsToNodes (refs-ветка) — Task 11.
- ✅ Engine: department_name field + JOIN + endpoint — Tasks 1, 2, 3.
- ✅ Engine test — Task 4.
- ✅ MessageBubble integration (обе ветки + удалить wireToDisplay) — Task 13.
- ✅ Chat-страницы обёрнуты в Provider — Task 14.
- ✅ Кэш через Map в Provider — Task 6.
- ✅ Lazy fetch — Task 6 (openMention проверяет cache.has/loading.has).
- ✅ Esc / click-outside / scroll закрытие — Tasks 6, 7.
- ✅ Manual E2E — Task 15.

**Placeholder scan:** все шаги содержат конкретный код или конкретные команды. Никаких TBD/«handle case»/«similar to Task N».

**Type consistency:**
- `MentionKind = 'user' | 'role'` — определён в types.ts (Task 5), используется во всех других модулях.
- `UserData` поля (departmentName и др.) — определены в types.ts, маппятся из engine response в Provider'е (Task 6), читаются в Popup'е (Task 7).
- `ActivePopup` (username, kind, anchor) — types.ts (Task 5), создаётся в Provider (Task 6), читается в Popup (Task 7).
- `MentionLink` сигнатура `{ username, kind?, children }` — Task 8, вызывается consistent в MentionRenderer (Task 10) и parseMentionsToNodes (Task 11).
- `remarkMentions` создаёт ноды с `hProperties: { username, kind }` — Task 9; MentionRenderer (Task 10) принимает `{ username, kind }` props (react-markdown пробрасывает hProperties как props).

Готово.
