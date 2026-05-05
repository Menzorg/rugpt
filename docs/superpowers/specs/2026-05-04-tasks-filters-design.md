# Tasks Page — Filters & Sorting Design

## Goal

Добавить на страницу `/tasks` (как mobile, так и desktop) фильтры и сортировку. UX
должен быть удобен на обеих платформах.

## Scope

**Filters (все сразу):**
- Поиск по `title` (text)
- Статус (pending / in_progress / awaiting_review / done / overdue / created)
- Приоритет (1 / 2 / 3)
- Проект (`projectId`)
- Создатель (`creatorId`)
- Исполнитель (`assigneeId`)
- Дедлайн (range: from / to)

**Sorting** (single field):
- По дедлайну
- По приоритету
- По дате создания
- По названию
- По статусу

Direction cycles `asc → desc → asc` (без «none» состояния).

**State sharing:** фильтры/сортировка **сохраняются между табами** (общий state).

**Persistence:** local `useState` (без URL params, без localStorage). При reload —
сбрасывается. Если потребуется — добавим URL persistence в отдельной итерации.

## Architecture

### Hook

`src/app/hooks/useTaskFilters.ts`:

```ts
type SortField = 'deadline' | 'priority' | 'createdAt' | 'title' | 'status';
type SortOrder = 'asc' | 'desc';

interface TaskFiltersState {
  search: string;
  status: string;        // '' = all
  priority: number | '';  // '' = all
  projectId: string;     // '' = all
  creatorId: string;
  assigneeId: string;
  deadlineFrom: string | null;  // ISO date
  deadlineTo: string | null;
  sortField: SortField;
  sortOrder: SortOrder;
}

function useTaskFilters(tasks: Task[]): {
  filters: TaskFiltersState;
  setFilter<K extends keyof TaskFiltersState>(key: K, value: TaskFiltersState[K]): void;
  resetFilters(): void;
  toggleSort(field: SortField): void;        // click-header — cycle asc/desc
  filteredTasks: Task[];                     // memoized result
  activeFilterCount: number;                 // для «Ещё фильтры (n)» badge
}
```

Defaults:
```ts
const DEFAULT_FILTERS: TaskFiltersState = {
  search: '',
  status: '',
  priority: '',
  projectId: '',
  creatorId: '',
  assigneeId: '',
  deadlineFrom: null,
  deadlineTo: null,
  sortField: 'createdAt',
  sortOrder: 'desc',
};
```

`activeFilterCount` считает не-default не-search/не-status фильтры (priority,
projectId, creatorId, assigneeId, deadlineFrom/To). Search и status видны
отдельно в верхнем ряду на mobile — отдельно от counter.

`filteredTasks` мемоизирован через `useMemo` на `[tasks, filters]`. Чистые
predicates, single sort.

### UI components

1. **`SortableHeader.tsx`** — `<th>` обёртка для desktop таблицы:
   ```tsx
   <SortableHeader field="deadline" current={sort} onClick={toggleSort}>Дедлайн</SortableHeader>
   ```
   Рендерит заголовок + стрелку (↑ / ↓ / отсутствует если поле не активное). Click
   — `toggleSort('deadline')`.

2. **`TaskFiltersBar.tsx`** (mobile, `md:hidden`):
   - Ряд 1: status-chips горизонтальный скролл — «Все» + chip per status. Цвета
     совпадают с `STATUS_COLORS` бейджей в `tasks/page.tsx:46-50` (зелёный для
     `done`, primary для `in_progress`, yellow для `awaiting_review`, red для
     `overdue`, серый для `created`).
   - Ряд 2: search input full-width + sort-dropdown справа компактно (показывает
     текущее поле + стрелку направления).
   - Ряд 3: «Ещё фильтры (n)» button (n = `activeFilterCount`).

3. **`TaskFiltersSheet.tsx`** (mobile bottom-sheet, открывается из «Ещё фильтры»):
   - Приоритет: 3 chip'а (Срочно / Важно / Обычно) с цветами как `PRIORITY_LABELS`
     в `tasks/page.tsx:52-56`.
   - Проект: select (все проекты из `useProjects`).
   - Создатель: select (все юзеры орг).
   - Исполнитель: select.
   - Deadline-range: две `<DateTimePicker>` — «От» / «До» (используется существующий
     компонент из `app/components`).
   - Кнопка «Сбросить» внизу sheet.

4. **`TaskFiltersDesktop.tsx`** (desktop filter-row, `hidden md:block`):
   - Toggle button «Фильтры» рядом с tabs (рядом с «+ Создать задачу») —
     показывает/скрывает filter-row.
   - При open: ряд `<tr>` под `<thead>` с input/select под каждой колонкой:
     - Под «Название» — text search.
     - Под «Статус», «Приоритет», «Проект» — selects.
     - Под «Дедлайн» — два `<DateTimePicker>` (от/до) — компонент уже есть в проекте (используется в `tasks/page.tsx` для редактирования срока).
     - Под колонкой «Создатель»/«Исполнитель» (надпись зависит от tab) — **один select**, который при tab=my/participating фильтрует по `creator`, иначе по `assignee`. Логика выбора привязана к табу.
     - Под «Действия» — кнопка «×» (сброс всех).

5. **Active filter chips на mobile** (над cards, под `TaskFiltersBar`):
   - Показывают активные не-status / не-search фильтры (priority, project,
     creator, assignee, deadline-range).
   - Каждый chip с `×` — убирает один фильтр.
   - Если есть хотя бы один активный — справа кнопка «Сбросить всё».

### Integration in `tasks/page.tsx`

```tsx
const { tasks, ... } = useTasks();
const { filters, setFilter, resetFilters, toggleSort, filteredTasks, activeFilterCount } =
  useTaskFilters(tasks);

// Mobile: <TaskFiltersBar /> + <TaskFiltersSheet /> + active chips
// Desktop: toggle button + <TaskFiltersDesktop /> + <SortableHeader> в thead

// Все места где сейчас .map(tasks) → .map(filteredTasks)
```

Tabs (Мои/Участвую/Назначенные/Все/Архив) применяются к **`tasks`** (как сейчас —
backend filtering по табу), а `useTaskFilters` дополнительно фильтрует **поверх**.

## Status chip colors

```ts
// Из tasks/page.tsx STATUS_COLORS (theme-aware: primary=blue в light, orange в dark)
const STATUS_CHIP_COLORS = {
  created: 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
  in_progress: 'bg-primary/20 text-primary dark:bg-primary/20 dark:text-primary-light',
  awaiting_review: 'bg-yellow-200 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-200',
  done: 'bg-green-200 text-green-800 dark:bg-gray-700 dark:text-gray-400',
  overdue: 'bg-red-200 text-red-800 dark:bg-red-900/40 dark:text-red-200',
};
const STATUS_CHIP_LABELS = {
  created: 'Создано',
  in_progress: 'В работе',
  awaiting_review: 'На проверке',
  done: 'Сделано',
  overdue: 'Просрочено',
};
```

«Все» chip — нейтральный `bg-gray-100 dark:bg-gray-700` без счётчика (можно
добавить total count позже).

## Sort algorithm

```ts
function sortTasks(arr: Task[], field: SortField, order: SortOrder): Task[] {
  const dir = order === 'asc' ? 1 : -1;
  return [...arr].sort((a, b) => {
    switch (field) {
      case 'deadline':
        return ((a.deadline ?? '9999') > (b.deadline ?? '9999') ? 1 : -1) * dir;
      case 'priority':
        return (a.priority - b.priority) * dir;
      case 'createdAt':
        return (a.createdAt > b.createdAt ? 1 : -1) * dir;
      case 'title':
        return a.title.localeCompare(b.title) * dir;
      case 'status':
        return (STATUS_ORDER[a.status] - STATUS_ORDER[b.status]) * dir;
    }
  });
}

const STATUS_ORDER: Record<string, number> = {
  overdue: 0, awaiting_review: 1, in_progress: 2, created: 3, done: 4,
};
```

Tasks без `deadline` — отправляются в конец (сортировкой `'9999'`).

## Filter predicates

```ts
function matches(task: Task, f: TaskFiltersState): boolean {
  if (f.search && !task.title.toLowerCase().includes(f.search.toLowerCase())) return false;
  if (f.status && task.status !== f.status) return false;
  if (f.priority !== '' && task.priority !== f.priority) return false;
  if (f.projectId && task.project_id !== f.projectId) return false;
  if (f.creatorId && task.creator?.id !== f.creatorId) return false;
  if (f.assigneeId && task.assignee?.id !== f.assigneeId) return false;
  if (f.deadlineFrom && (!task.deadline || task.deadline < f.deadlineFrom)) return false;
  if (f.deadlineTo && (!task.deadline || task.deadline > f.deadlineTo)) return false;
  return true;
}
```

## Что НЕ делаем (out of scope)

- URL params persistence
- localStorage persistence
- Server-side фильтрация (engine /tasks?status=...)
- Multi-select для status / priority (только single value «или all»)
- Сохранённые пресеты («Мои просроченные», «Срочные на этой неделе»)
- Фильтр по тегам / меткам (их пока нет в модели)

## Files to create / modify

**Create:**
- `packages/frontend/src/app/hooks/useTaskFilters.ts`
- `packages/frontend/src/app/components/TaskFiltersBar.tsx`
- `packages/frontend/src/app/components/TaskFiltersSheet.tsx`
- `packages/frontend/src/app/components/TaskFiltersDesktop.tsx`
- `packages/frontend/src/app/components/SortableHeader.tsx`

**Modify:**
- `packages/frontend/src/app/tasks/page.tsx` — интеграция: hook, рендер
  filter-bar/desktop под tabs, замена `tasks.map` → `filteredTasks.map`,
  замена `<th>...</th>` на `<SortableHeader>` в desktop таблице.

## Testing (manual E2E checklist)

- Mobile:
  - [ ] Status-chips переключают фильтр (один tap)
  - [ ] Search input фильтрует по title
  - [ ] Sort-dropdown меняет поле и направление
  - [ ] «Ещё фильтры (n)» открывает sheet
  - [ ] В sheet: все 5 фильтров работают
  - [ ] Active chips с `×` убирают конкретный фильтр
  - [ ] «Сбросить всё» возвращает дефолт
- Desktop:
  - [ ] Click на th сортирует, стрелка меняется
  - [ ] Toggle «Фильтры» показывает/скрывает row
  - [ ] Inputs/selects под каждой колонкой работают
  - [ ] Date-range фильтрует
  - [ ] «×» под «Действия» сбрасывает
- Cross-tab:
  - [ ] Фильтры сохраняются при переключении табов
  - [ ] Reload — фильтры сбрасываются
- Edge:
  - [ ] Пустой результат — показывается заглушка «Нет задач»
  - [ ] Tasks без deadline — корректно сортируются (в конце)
