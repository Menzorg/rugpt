# Tasks Page Filters & Sorting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить фильтры (search/status/priority/project/creator/assignee/deadline-range) и сортировку (deadline/priority/createdAt/title/status) на страницу `/tasks`, удобно работающие на mobile и desktop.

**Architecture:** Hook `useTaskFilters` управляет state и возвращает уже отфильтрованный/сортированный массив. UI разнесён на 4 атомарных компонента (`SortableHeader`, `TaskFiltersBar`, `TaskFiltersSheet`, `TaskFiltersDesktop`). Mobile показывает chips-bar + bottom-sheet, desktop — click-header sort + filter-row под `<thead>`. Фильтрация client-side, мемоизирована на `[tasks, filters]`.

**Tech Stack:** React 19, TypeScript, Tailwind CSS, Next.js App Router, существующие хуки `useTasks`/`useUsers`/`useDepartments`/`useProjects`, компонент `<DateTimePicker>`.

**Constraints:**
- Frontend-проект **не использует** TDD/jest/vitest — валидация каждой задачи через `npx tsc --noEmit` + ручная визуальная проверка.
- **Без git-коммитов в шагах плана** — пользователь коммитит сам, когда сочтёт нужным.

---

## File Structure

**Create:**
- `packages/frontend/src/app/hooks/useTaskFilters.ts` — единственный hook со state + filter/sort logic. Один файл, чтобы вся логика была в одном месте.
- `packages/frontend/src/app/components/SortableHeader.tsx` — `<th>` обёртка с click-sort + стрелкой. Одна узкая ответственность.
- `packages/frontend/src/app/components/TaskFiltersBar.tsx` — mobile bar: status-chips + search + sort-dropdown + «Ещё фильтры» button.
- `packages/frontend/src/app/components/TaskFiltersSheet.tsx` — mobile bottom-sheet с приоритет/проект/создатель/исполнитель/deadline.
- `packages/frontend/src/app/components/TaskFiltersDesktop.tsx` — desktop filter-row под `<thead>`.

**Modify:**
- `packages/frontend/src/app/tasks/page.tsx` — интеграция: подключить hook, добавить toggle-кнопку «Фильтры» на desktop, заменить `<th>...</th>` на `<SortableHeader>`, добавить mobile bar над cards, заменить `tasks.map(...)` на `filteredTasks.map(...)`.

Каждый файл — одна чёткая ответственность. Hook и UI разнесены по принципу container/presentational.

---

## Task 1: useTaskFilters hook

**Files:**
- Create: `packages/frontend/src/app/hooks/useTaskFilters.ts`

- [ ] **Step 1: Создать файл с типами и дефолтами**

Содержимое:

```ts
import { useMemo, useState, useCallback } from 'react';

export type TaskSortField = 'deadline' | 'priority' | 'createdAt' | 'title' | 'status';
export type TaskSortOrder = 'asc' | 'desc';

export interface TaskFiltersState {
  search: string;
  status: string;            // '' = all
  priority: number | '';     // '' = all
  projectId: string;         // '' = all
  creatorId: string;         // '' = all
  assigneeId: string;        // '' = all
  deadlineFrom: string | null; // ISO (YYYY-MM-DD or full ISO)
  deadlineTo: string | null;
  sortField: TaskSortField;
  sortOrder: TaskSortOrder;
}

export const DEFAULT_TASK_FILTERS: TaskFiltersState = {
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

// Минимальный shape Task, который нужен hook'у — совместим с TaskRow в tasks/page.tsx.
export interface FilterableTask {
  id: string;
  title: string;
  status: string;
  priority: number;
  deadline?: string | null;
  createdAt?: string;          // engine иногда возвращает в snake — тогда страница маппит наверх
  project_id?: string | null;
  creator?: { id: string } | null;
  assignee?: { id: string } | null;
}

const STATUS_ORDER: Record<string, number> = {
  overdue: 0,
  awaiting_review: 1,
  in_progress: 2,
  created: 3,
  done: 4,
};

function matches(task: FilterableTask, f: TaskFiltersState): boolean {
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

function compare(a: FilterableTask, b: FilterableTask, field: TaskSortField, order: TaskSortOrder): number {
  const dir = order === 'asc' ? 1 : -1;
  switch (field) {
    case 'deadline': {
      const av = a.deadline ?? '￿';
      const bv = b.deadline ?? '￿';
      return av === bv ? 0 : (av > bv ? 1 : -1) * dir;
    }
    case 'priority':
      return (a.priority - b.priority) * dir;
    case 'createdAt': {
      const av = a.createdAt ?? '';
      const bv = b.createdAt ?? '';
      return av === bv ? 0 : (av > bv ? 1 : -1) * dir;
    }
    case 'title':
      return a.title.localeCompare(b.title) * dir;
    case 'status':
      return ((STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99)) * dir;
  }
}

export function useTaskFilters<T extends FilterableTask>(tasks: T[]) {
  const [filters, setFilters] = useState<TaskFiltersState>(DEFAULT_TASK_FILTERS);

  const setFilter = useCallback(<K extends keyof TaskFiltersState>(key: K, value: TaskFiltersState[K]) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }, []);

  const resetFilters = useCallback(() => {
    setFilters(DEFAULT_TASK_FILTERS);
  }, []);

  const toggleSort = useCallback((field: TaskSortField) => {
    setFilters((prev) => {
      if (prev.sortField !== field) {
        return { ...prev, sortField: field, sortOrder: 'asc' };
      }
      return { ...prev, sortOrder: prev.sortOrder === 'asc' ? 'desc' : 'asc' };
    });
  }, []);

  const filteredTasks = useMemo(() => {
    const arr = tasks.filter((t) => matches(t, filters));
    arr.sort((a, b) => compare(a, b, filters.sortField, filters.sortOrder));
    return arr;
  }, [tasks, filters]);

  // activeFilterCount — счётчик не-default НЕ-search/НЕ-status фильтров
  // (search и status видны на верхнем ряду на mobile, отдельно от counter).
  const activeFilterCount = useMemo(() => {
    let n = 0;
    if (filters.priority !== '') n++;
    if (filters.projectId) n++;
    if (filters.creatorId) n++;
    if (filters.assigneeId) n++;
    if (filters.deadlineFrom || filters.deadlineTo) n++;
    return n;
  }, [filters]);

  return { filters, setFilter, resetFilters, toggleSort, filteredTasks, activeFilterCount };
}
```

- [ ] **Step 2: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0, без ошибок.

---

## Task 2: SortableHeader component

**Files:**
- Create: `packages/frontend/src/app/components/SortableHeader.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { TaskSortField, TaskSortOrder } from '../hooks/useTaskFilters';

interface SortableHeaderProps {
  field: TaskSortField;
  currentField: TaskSortField;
  currentOrder: TaskSortOrder;
  onClick: (field: TaskSortField) => void;
  children: React.ReactNode;
  align?: 'left' | 'right';
}

export function SortableHeader({ field, currentField, currentOrder, onClick, children, align = 'left' }: SortableHeaderProps) {
  const isActive = currentField === field;
  const arrow = !isActive ? '' : currentOrder === 'asc' ? '▲' : '▼';
  const alignClass = align === 'right' ? 'text-right' : 'text-left';
  return (
    <th className={`px-4 py-3 ${alignClass} text-xs font-medium text-gray-500 uppercase`}>
      <button
        type="button"
        onClick={() => onClick(field)}
        className="inline-flex items-center gap-1 uppercase hover:text-gray-700 dark:hover:text-gray-300 select-none"
      >
        {children}
        {arrow && <span className="text-[10px] text-primary">{arrow}</span>}
      </button>
    </th>
  );
}
```

- [ ] **Step 2: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0.

---

## Task 3: TaskFiltersBar (mobile)

**Files:**
- Create: `packages/frontend/src/app/components/TaskFiltersBar.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { TaskFiltersState, TaskSortField, TaskSortOrder } from '../hooks/useTaskFilters';

const STATUS_CHIP_COLORS: Record<string, string> = {
  created: 'bg-gray-200 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
  in_progress: 'bg-primary/20 text-primary dark:bg-primary/20 dark:text-primary-light',
  awaiting_review: 'bg-yellow-200 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-200',
  done: 'bg-green-200 text-green-800 dark:bg-gray-700 dark:text-gray-400',
  overdue: 'bg-red-200 text-red-800 dark:bg-red-900/40 dark:text-red-200',
};

const STATUS_CHIP_LABELS: Record<string, string> = {
  created: 'Создано',
  in_progress: 'В работе',
  awaiting_review: 'На проверке',
  done: 'Сделано',
  overdue: 'Просрочено',
};

const SORT_LABELS: Record<TaskSortField, string> = {
  deadline: 'Дедлайн',
  priority: 'Приоритет',
  createdAt: 'Дата создания',
  title: 'Название',
  status: 'Статус',
};

interface TaskFiltersBarProps {
  filters: TaskFiltersState;
  setFilter: <K extends keyof TaskFiltersState>(key: K, value: TaskFiltersState[K]) => void;
  toggleSort: (field: TaskSortField) => void;
  activeFilterCount: number;
  onOpenSheet: () => void;
}

export function TaskFiltersBar({ filters, setFilter, toggleSort, activeFilterCount, onOpenSheet }: TaskFiltersBarProps) {
  const statuses: string[] = ['created', 'in_progress', 'awaiting_review', 'done', 'overdue'];

  return (
    <div className="md:hidden flex flex-col gap-2 mb-3">
      {/* Status chips horizontal scroll */}
      <div className="flex gap-2 overflow-x-auto pb-1 -mx-1 px-1">
        <button
          type="button"
          onClick={() => setFilter('status', '')}
          className={`shrink-0 px-3 py-1 text-xs rounded-full border transition-colors
                     ${filters.status === ''
                       ? 'bg-gray-300 text-gray-800 border-gray-400 dark:bg-gray-600 dark:text-white dark:border-gray-500'
                       : 'bg-gray-100 text-gray-600 border-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:border-gray-700'}`}
        >
          Все
        </button>
        {statuses.map((s) => {
          const isActive = filters.status === s;
          const colorClass = STATUS_CHIP_COLORS[s] || '';
          return (
            <button
              key={s}
              type="button"
              onClick={() => setFilter('status', isActive ? '' : s)}
              className={`shrink-0 px-3 py-1 text-xs rounded-full border transition-colors ${colorClass}
                         ${isActive ? 'ring-2 ring-offset-1 ring-primary' : 'opacity-70'}`}
            >
              {STATUS_CHIP_LABELS[s]}
            </button>
          );
        })}
      </div>

      {/* Search + sort dropdown */}
      <div className="flex gap-2">
        <input
          type="text"
          value={filters.search}
          onChange={(e) => setFilter('search', e.target.value)}
          placeholder="Поиск по названию"
          className="flex-1 px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-primary"
        />
        <select
          value={`${filters.sortField}:${filters.sortOrder}`}
          onChange={(e) => {
            const [field, order] = e.target.value.split(':') as [TaskSortField, TaskSortOrder];
            // двойной вызов: сначала сменить поле, потом подогнать направление
            if (filters.sortField !== field) toggleSort(field);
            // если order совпадает — больше ничего; иначе ещё раз toggle
            if (filters.sortOrder !== order) toggleSort(field);
          }}
          className="px-2 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-primary"
          aria-label="Сортировка"
        >
          {(Object.keys(SORT_LABELS) as TaskSortField[]).map((f) => (
            <optgroup key={f} label={SORT_LABELS[f]}>
              <option value={`${f}:asc`}>{SORT_LABELS[f]} ▲</option>
              <option value={`${f}:desc`}>{SORT_LABELS[f]} ▼</option>
            </optgroup>
          ))}
        </select>
      </div>

      {/* "Ещё фильтры (n)" button */}
      <button
        type="button"
        onClick={onOpenSheet}
        className="self-start px-3 py-1.5 text-xs rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
      >
        Ещё фильтры{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}
      </button>
    </div>
  );
}
```

- [ ] **Step 2: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0.

---

## Task 4: TaskFiltersSheet (mobile bottom-sheet)

**Files:**
- Create: `packages/frontend/src/app/components/TaskFiltersSheet.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { TaskFiltersState } from '../hooks/useTaskFilters';
import { DateTimePicker } from './DateTimePicker';

interface ListUser { id: string; name: string }
interface ListProject { id: string; name: string }

interface TaskFiltersSheetProps {
  open: boolean;
  onClose: () => void;
  filters: TaskFiltersState;
  setFilter: <K extends keyof TaskFiltersState>(key: K, value: TaskFiltersState[K]) => void;
  resetFilters: () => void;
  projects: ListProject[];
  users: ListUser[];
}

const PRIORITY_OPTIONS = [
  { value: 3, label: 'Срочно',  color: 'bg-red-100 text-red-700 border-red-300 dark:bg-red-900/30 dark:text-red-300 dark:border-red-700' },
  { value: 2, label: 'Важно',   color: 'bg-yellow-100 text-yellow-700 border-yellow-300 dark:bg-yellow-900/30 dark:text-yellow-300 dark:border-yellow-700' },
  { value: 1, label: 'Обычно',  color: 'bg-gray-100 text-gray-700 border-gray-300 dark:bg-gray-700 dark:text-gray-300 dark:border-gray-600' },
];

export function TaskFiltersSheet({ open, onClose, filters, setFilter, resetFilters, projects, users }: TaskFiltersSheetProps) {
  if (!open) return null;

  return (
    <>
      <div className="md:hidden fixed inset-0 bg-black/50 z-40" onClick={onClose} />
      <div className="md:hidden fixed left-0 right-0 bottom-0 z-50 bg-white dark:bg-gray-800 rounded-t-2xl p-4 max-h-[85vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-semibold text-gray-900 dark:text-white">Фильтры</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
          >
            Закрыть
          </button>
        </div>

        <div className="space-y-4">
          {/* Priority chips */}
          <div>
            <div className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">Приоритет</div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setFilter('priority', '')}
                className={`px-3 py-1 text-xs rounded-full border transition-colors
                           ${filters.priority === ''
                             ? 'bg-gray-300 text-gray-800 border-gray-400 dark:bg-gray-600 dark:text-white dark:border-gray-500'
                             : 'bg-gray-100 text-gray-600 border-gray-200 dark:bg-gray-700 dark:text-gray-400 dark:border-gray-600'}`}
              >
                Все
              </button>
              {PRIORITY_OPTIONS.map((opt) => {
                const isActive = filters.priority === opt.value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setFilter('priority', isActive ? '' : opt.value)}
                    className={`px-3 py-1 text-xs rounded-full border transition-colors ${opt.color}
                               ${isActive ? 'ring-2 ring-offset-1 ring-primary' : 'opacity-70'}`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Project select */}
          <div>
            <label className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">Проект</label>
            <select
              value={filters.projectId}
              onChange={(e) => setFilter('projectId', e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="">Все проекты</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>

          {/* Creator select */}
          <div>
            <label className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">Создатель</label>
            <select
              value={filters.creatorId}
              onChange={(e) => setFilter('creatorId', e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="">Все</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>{u.name}</option>
              ))}
            </select>
          </div>

          {/* Assignee select */}
          <div>
            <label className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">Исполнитель</label>
            <select
              value={filters.assigneeId}
              onChange={(e) => setFilter('assigneeId', e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="">Все</option>
              {users.map((u) => (
                <option key={u.id} value={u.id}>{u.name}</option>
              ))}
            </select>
          </div>

          {/* Deadline range */}
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">Дедлайн от</label>
              <DateTimePicker
                value={filters.deadlineFrom || ''}
                onChange={(v) => setFilter('deadlineFrom', v ? new Date(v).toISOString() : null)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-2 py-1.5 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
              />
            </div>
            <div>
              <label className="text-xs font-medium text-gray-700 dark:text-gray-300 mb-1 block">До</label>
              <DateTimePicker
                value={filters.deadlineTo || ''}
                onChange={(v) => setFilter('deadlineTo', v ? new Date(v).toISOString() : null)}
                className="w-full border border-gray-300 dark:border-gray-600 rounded-md px-2 py-1.5 text-sm bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
              />
            </div>
          </div>

          {/* Reset all */}
          <div className="flex justify-between pt-2">
            <button
              type="button"
              onClick={resetFilters}
              className="px-4 py-2 text-sm rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
            >
              Сбросить всё
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm rounded-md bg-primary hover:bg-primary-dark text-white"
            >
              Применить
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
```

- [ ] **Step 2: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0.

**Note:** Если `<DateTimePicker>` ожидает другой тип `onChange` (string vs ISO), смотри сигнатуру компонента в `packages/frontend/src/app/components/DateTimePicker.tsx` и адаптируй `setFilter('deadlineFrom', ...)` соответственно (без своей конверсии, если компонент сам отдаёт ISO).

---

## Task 5: TaskFiltersDesktop (filter-row под thead)

**Files:**
- Create: `packages/frontend/src/app/components/TaskFiltersDesktop.tsx`

- [ ] **Step 1: Создать файл**

```tsx
'use client';

import type { TaskFiltersState } from '../hooks/useTaskFilters';
import { DateTimePicker } from './DateTimePicker';

interface ListUser { id: string; name: string }
interface ListProject { id: string; name: string }

interface TaskFiltersDesktopProps {
  filters: TaskFiltersState;
  setFilter: <K extends keyof TaskFiltersState>(key: K, value: TaskFiltersState[K]) => void;
  resetFilters: () => void;
  projects: ListProject[];
  users: ListUser[];
  // 'creator' для табов my/participating, 'assignee' для остальных
  participantField: 'creator' | 'assignee';
}

const STATUS_OPTIONS = [
  { value: '', label: 'Все' },
  { value: 'created', label: 'Создано' },
  { value: 'in_progress', label: 'В работе' },
  { value: 'awaiting_review', label: 'На проверке' },
  { value: 'done', label: 'Сделано' },
  { value: 'overdue', label: 'Просрочено' },
];

const PRIORITY_OPTIONS = [
  { value: '', label: 'Все' },
  { value: 3, label: 'Срочно' },
  { value: 2, label: 'Важно' },
  { value: 1, label: 'Обычно' },
];

const inputClass = 'w-full px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:outline-none focus:ring-1 focus:ring-primary';

export function TaskFiltersDesktopRow({ filters, setFilter, resetFilters, projects, users, participantField }: TaskFiltersDesktopProps) {
  // Возвращает tr — встраивается внутрь tbody-like структуры, но рендерится
  // визуально под <thead>. В нашей таблице мы кладём этот <tr> в отдельный
  // <thead className="bg-white dark:bg-gray-800"> чтобы не нарушить структуру tbody.
  const participantValue = participantField === 'creator' ? filters.creatorId : filters.assigneeId;
  const setParticipantValue = (v: string) => {
    if (participantField === 'creator') setFilter('creatorId', v);
    else setFilter('assigneeId', v);
  };

  return (
    <tr className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
      <th className="px-4 py-2">
        <input
          type="text"
          value={filters.search}
          onChange={(e) => setFilter('search', e.target.value)}
          placeholder="Поиск..."
          className={inputClass}
        />
      </th>
      <th className="px-4 py-2">
        <select value={filters.status} onChange={(e) => setFilter('status', e.target.value)} className={inputClass}>
          {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </th>
      <th className="px-4 py-2">
        <select
          value={filters.priority === '' ? '' : String(filters.priority)}
          onChange={(e) => setFilter('priority', e.target.value === '' ? '' : Number(e.target.value))}
          className={inputClass}
        >
          {PRIORITY_OPTIONS.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
        </select>
      </th>
      <th className="px-4 py-2">
        <div className="flex gap-1">
          <DateTimePicker
            value={filters.deadlineFrom || ''}
            onChange={(v) => setFilter('deadlineFrom', v ? new Date(v).toISOString() : null)}
            className={inputClass}
          />
          <DateTimePicker
            value={filters.deadlineTo || ''}
            onChange={(v) => setFilter('deadlineTo', v ? new Date(v).toISOString() : null)}
            className={inputClass}
          />
        </div>
      </th>
      <th className="px-4 py-2">
        <select value={filters.projectId} onChange={(e) => setFilter('projectId', e.target.value)} className={inputClass}>
          <option value="">Все</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </th>
      <th className="px-4 py-2">
        <select value={participantValue} onChange={(e) => setParticipantValue(e.target.value)} className={inputClass}>
          <option value="">Все</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
        </select>
      </th>
      <th className="px-4 py-2 text-right">
        <button
          type="button"
          onClick={resetFilters}
          title="Сбросить все фильтры"
          className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-white text-sm"
        >
          ✕
        </button>
      </th>
    </tr>
  );
}
```

- [ ] **Step 2: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0.

---

## Task 6: Интеграция в tasks/page.tsx

**Files:**
- Modify: `packages/frontend/src/app/tasks/page.tsx`

- [ ] **Step 1: Добавить imports в начало файла**

В блок импортов добавить:

```tsx
import { useTaskFilters } from '../hooks/useTaskFilters';
import { SortableHeader } from '../components/SortableHeader';
import { TaskFiltersBar } from '../components/TaskFiltersBar';
import { TaskFiltersSheet } from '../components/TaskFiltersSheet';
import { TaskFiltersDesktopRow } from '../components/TaskFiltersDesktop';
```

- [ ] **Step 2: Подключить hook в компоненте**

После `const { tasks, ... } = useTasks();` (или эквивалента) добавить:

```tsx
const {
  filters,
  setFilter,
  resetFilters,
  toggleSort,
  filteredTasks,
  activeFilterCount,
} = useTaskFilters(tasks as TaskRow[]);
const [sheetOpen, setSheetOpen] = useState(false);
const [desktopFiltersOpen, setDesktopFiltersOpen] = useState(false);

// Список юзеров для select'ов в фильтрах. Берём из существующего useUsers().
// users уже загружены — переиспользуем без доп.запроса.
const filterUsersList = useMemo(
  () => users.map((u) => ({ id: u.id, name: u.name })),
  [users],
);
const filterProjectsList = useMemo(
  () => projects.map((p) => ({ id: p.id, name: p.name })),
  [projects],
);
const participantField: 'creator' | 'assignee' = (tab === 'my' || tab === 'participating') ? 'creator' : 'assignee';
```

Если `users` или `projects` ещё не загружены через `useUsers()`/`useProjects()` в этом файле — добавить вызовы. Проверь существующие импорты — вероятно `useProjects` уже подключён (см. project_id рендер). Если нет — добавь:

```tsx
import { useUsers } from '../hooks/useUsers';
import { useProjects } from '../hooks/useProjects';

// ... в компоненте:
const { users } = useUsers();
const { projects } = useProjects();
```

- [ ] **Step 3: Добавить mobile filter bar над списком cards**

В `tasks/page.tsx` найти место где рендерится mobile cards (`<div className="md:hidden ...">` или похоже). Перед этим блоком добавить:

```tsx
<TaskFiltersBar
  filters={filters}
  setFilter={setFilter}
  toggleSort={toggleSort}
  activeFilterCount={activeFilterCount}
  onOpenSheet={() => setSheetOpen(true)}
/>
<TaskFiltersSheet
  open={sheetOpen}
  onClose={() => setSheetOpen(false)}
  filters={filters}
  setFilter={setFilter}
  resetFilters={resetFilters}
  projects={filterProjectsList}
  users={filterUsersList}
/>
```

- [ ] **Step 4: Добавить кнопку «Фильтры» (desktop) рядом с tabs**

Возле блока с табами (Мои/Участвую/...) добавить кнопку:

```tsx
<button
  type="button"
  onClick={() => setDesktopFiltersOpen((v) => !v)}
  className="hidden md:inline-flex items-center px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700"
>
  Фильтры{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}
</button>
```

Можно поместить рядом с «+ Создать задачу» (см. строку 740 текущего файла).

- [ ] **Step 5: Заменить `<th>` на `<SortableHeader>` в desktop таблице**

В `<thead>` desktop-таблицы (примерно строки 882-893 текущего файла) заменить:

```tsx
<thead className="bg-gray-50 dark:bg-gray-900">
  <tr>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Название</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Статус</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Приоритет</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Дедлайн</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Проект</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
      {tab === 'my' || tab === 'participating' ? 'Создатель' : 'Исполнитель'}
    </th>
    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Действия</th>
  </tr>
</thead>
```

на:

```tsx
<thead className="bg-gray-50 dark:bg-gray-900">
  <tr>
    <SortableHeader field="title" currentField={filters.sortField} currentOrder={filters.sortOrder} onClick={toggleSort}>Название</SortableHeader>
    <SortableHeader field="status" currentField={filters.sortField} currentOrder={filters.sortOrder} onClick={toggleSort}>Статус</SortableHeader>
    <SortableHeader field="priority" currentField={filters.sortField} currentOrder={filters.sortOrder} onClick={toggleSort}>Приоритет</SortableHeader>
    <SortableHeader field="deadline" currentField={filters.sortField} currentOrder={filters.sortOrder} onClick={toggleSort}>Дедлайн</SortableHeader>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Проект</th>
    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
      {tab === 'my' || tab === 'participating' ? 'Создатель' : 'Исполнитель'}
    </th>
    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Действия</th>
  </tr>
  {desktopFiltersOpen && (
    <TaskFiltersDesktopRow
      filters={filters}
      setFilter={setFilter}
      resetFilters={resetFilters}
      projects={filterProjectsList}
      users={filterUsersList}
      participantField={participantField}
    />
  )}
</thead>
```

Поля «Проект»/«Создатель/Исполнитель»/«Действия» оставлены НЕсортируемыми (они либо неоднозначны для sort, либо невалидны).

- [ ] **Step 6: Заменить `tasks.map(...)` на `filteredTasks.map(...)`**

В файле `tasks/page.tsx` найти все места где итерируются задачи для рендера:
- В mobile cards (`tasks.map((t) => ...)` или похоже)
- В desktop tbody (`tasks.map((t) => ...)`)

Заменить **только эти места рендера** на `filteredTasks`. Остальные места где `tasks` используется для других целей (counter, fetch logic) — НЕ менять.

Конкретно: в mobile cards renderer и в `<tbody>` desktop таблицы поменять источник массива.

- [ ] **Step 7: Сбросить sortField в createdAt при отображении архивных (опционально)**

Если в табе «Архив» дефолтная сортировка должна быть другой — НЕ делаем (out of scope). Все табы используют общий filters state с дефолтным sort `createdAt desc`.

- [ ] **Step 8: Проверить TypeScript**

Run: `cd /root/webclient_rugpt/packages/frontend && npx tsc --noEmit`
Expected: exit=0.

- [ ] **Step 9: Визуальная проверка (manual E2E)**

Запустить dev: `cd /root/webclient_rugpt && ./dev.sh` (если ещё не запущен).
Открыть `https://rugpt.pro/tasks` (или localhost:3000/tasks).

**Mobile (DevTools mobile view):**
- [ ] Status-chips видны под табами и горизонтально скроллятся
- [ ] Tap на chip фильтрует список (повторный tap снимает)
- [ ] Search-input фильтрует по title
- [ ] Sort-dropdown меняет поле и направление, список пересортируется
- [ ] «Ещё фильтры» открывает bottom-sheet
- [ ] В sheet: priority chips, project select, creator select, assignee select, deadline-range — все работают
- [ ] «Сбросить всё» возвращает дефолт
- [ ] «Применить» закрывает sheet
- [ ] Counter «(n)» соответствует числу активных доп.фильтров

**Desktop:**
- [ ] Кнопка «Фильтры» рядом с tabs — toggle filter-row под thead
- [ ] Click на th «Название»/«Статус»/«Приоритет»/«Дедлайн» — сортирует, стрелка ▲/▼ появляется на активной колонке
- [ ] Повторный click на ту же колонку — меняет направление
- [ ] При open: search-input под «Название», select под «Статус»/«Приоритет», два date-input под «Дедлайн», select под «Проект» и «Создатель/Исполнитель»
- [ ] «×» под «Действия» — сброс всех

**Cross-tab:**
- [ ] Установить фильтр (напр. status=in_progress) → переключить таб → фильтр сохраняется
- [ ] Reload страницы → фильтры сбрасываются (нет URL persistence — ожидаемо)

**Edge:**
- [ ] Пустой результат после фильтра — показывается заглушка (или просто пусто) корректно
- [ ] Tasks без deadline корректно сортируются (в конце при asc, в начале при desc, либо последовательно)
- [ ] Mobile filter-bar и desktop filter-row не появляются одновременно (правильные `md:hidden` / `hidden md:block`)

---

## Self-review notes

**Spec coverage:**
- [x] Search by title — Task 1 (matches), Task 3 (input), Task 5 (input)
- [x] Status filter — Task 1 (matches), Task 3 (chips), Task 5 (select)
- [x] Priority filter — Task 1 (matches), Task 4 (chips), Task 5 (select)
- [x] Project filter — Task 1, Task 4, Task 5
- [x] Creator filter — Task 1, Task 4, Task 5
- [x] Assignee filter — Task 1, Task 4, Task 5
- [x] Deadline-range — Task 1, Task 4, Task 5
- [x] Sort field+order — Task 1 (toggleSort/compare), Task 2 (SortableHeader), Task 3 (sort dropdown)
- [x] Active filter counter — Task 1 (activeFilterCount), Task 3 (badge)
- [x] State shared между табами — Task 1 (single hook instance в page)
- [x] No URL persistence — useState only ✓
- [x] Client-side filtering — useMemo on tasks ✓
- [x] Status chip colors совпадают с STATUS_COLORS — Task 3 (хардкод соответствует)
- [x] Priority chip colors соответствуют PRIORITY_LABELS — Task 4 (хардкод соответствует)
- [x] Mobile UX — Task 3+4
- [x] Desktop UX — Task 2+5
- [x] Reset all button — Task 4 (sheet) и Task 5 («×»)

**Type consistency:**
- `TaskFiltersState`, `TaskSortField`, `TaskSortOrder` — определены в Task 1, импортируются всюду
- `FilterableTask` совместим с `TaskRow` (id/title/status/priority/deadline/project_id/creator/assignee — всё есть в TaskRow page.tsx:20-34)
- `setFilter<K>(key, value)` — единая сигнатура во всех компонентах
- `participantField: 'creator' | 'assignee'` — Task 5 input, Task 6 derives from tab

**Что не покрыто планом и почему:**
- Тесты (jest/vitest) — фронт не имеет test infra, валидация через tsc + ручная E2E. Спека это допускает.
- URL persistence — out of scope.
- Server-side filter — out of scope.
- Если `<DateTimePicker>` имеет несовместимую сигнатуру — Task 4 имеет note про адаптацию.
