# Кликабельные упоминания (`@user` / `@@user`) — дизайн

**Дата:** 2026-05-04
**Статус:** утверждён пользователем

## Контекст

Сообщения отрисовываются в `MessageBubble` (`packages/frontend/src/app/components/MessageBubble.tsx`). На стороне input'а эмоджи 🧑/🤖 уже маппятся в `@`/`@@` перед отправкой (`textToWire`), а на стороне отображения сейчас работает обратный `wireToDisplay`: `@anna` → `🧑anna`, `@@chu` → `🤖chu`. Текст plain — без интерактивности. Markdown в сообщениях **остаётся** (нужен для AI-ответов с заголовками/списками/code-блоками).

Задача: упоминания кликабельны. По клику — popup с инфой о пользователе. Архитектура должна переиспользоваться на других страницах (задачи, отчёты, …) без копипасты.

## Архитектура

Три объекта в одном модуле `app/components/mentions/`:

- **`remarkMentions`** — remark-плагин для `react-markdown`. Парсит `@user`/`@@user` на уровне AST, создаёт ноды кастомного типа `mention` с `{ kind, username }`. Позволяет ReactMarkdown отрендерить mention как React-компонент через `components.mention` без href-хаков и encoding'а в строку.
- **`MentionsProvider`** — React Context provider. Хранит **один** активный popup на чат и **кэш** загруженных пользователей. Рендерит сам popup (одну штуку) поверх контента.
- **`MentionLink`** — тонкая кнопка-цель клика. Не имеет своего state и не делает fetch. По клику зовёт `openMention(username, kind, anchorEl)` из context'а.

### Почему так

- **Remark-плагин**: чистая интеграция с markdown без encoding-хаков. Markdown сохраняется. ~30-40 строк AST-обхода + map.
- **Один popup на чат**: естественный UX (нельзя случайно открыть два разных), меньше React-state'ов. Anchor для позиционирования передаётся в provider при клике.
- **Кэш**: один и тот же `@anna` упомянут 10 раз → fetch один раз. Map<username, UserData> в provider'е, очищается при unmount чата.
- **MentionLink тонкий**: легко переиспользовать в /tasks и пр. без знания о popup'е.

## Компоненты

### `remarkMentions.ts` — remark-плагин

```ts
import type { Plugin } from 'unified';
import type { Root, Text } from 'mdast';
import { visit } from 'unist-util-visit';

const MENTION_RE = /(^|\s)(@@?)(\w+)/g;

interface MentionNode {
  type: 'mention';
  kind: 'user' | 'role';
  username: string;
  data: { hName: 'mention' };
}

export const remarkMentions: Plugin<[], Root> = () => (tree) => {
  visit(tree, 'text', (node, index, parent) => {
    if (!parent || index == null) return;
    const matches = [...node.value.matchAll(MENTION_RE)];
    if (matches.length === 0) return;
    const newNodes: any[] = [];
    let cursor = 0;
    for (const m of matches) {
      const [full, prefix, at, username] = m;
      const start = m.index! + prefix.length;
      if (start > cursor) {
        newNodes.push({ type: 'text', value: node.value.slice(cursor, start) });
      }
      newNodes.push({
        type: 'mention',
        kind: at === '@@' ? 'role' : 'user',
        username,
        data: { hName: 'mention' },
      } as MentionNode);
      cursor = m.index! + full.length;
    }
    if (cursor < node.value.length) {
      newNodes.push({ type: 'text', value: node.value.slice(cursor) });
    }
    parent.children.splice(index, 1, ...newNodes);
    return [visit.SKIP, index + newNodes.length];
  });
};
```

В ReactMarkdown:
```tsx
<ReactMarkdown
  remarkPlugins={[remarkGfm, remarkMentions]}
  components={{ mention: MentionRenderer as any }}
>
  {text}
</ReactMarkdown>
```

`MentionRenderer({ node }: { node: MentionNode })` достаёт `kind`/`username` и рендерит `<MentionLink kind={kind} username={username}>{emoji}{username}</MentionLink>`.

Эмоджи `🧑`/`🤖` живёт **здесь** (внутри MentionRenderer'а), не в MentionLink. MentionLink остаётся generic.

### `MentionsProvider.tsx` — context + popup container

```tsx
interface MentionsContextValue {
  openMention: (username: string, kind: 'user' | 'role', anchor: DOMRect) => void;
  closeMention: () => void;
}

interface ActivePopup {
  username: string;
  kind: 'user' | 'role';
  anchor: DOMRect;
}

interface MentionsProviderProps {
  children: React.ReactNode;
}
```

State в provider'е:
- `activePopup: ActivePopup | null` — какой mention сейчас открыт.
- `cache: Map<string, UserData | { error: string }>` — данные/ошибки по username.
- `loadingUsernames: Set<string>` — что фетчится прямо сейчас.

`openMention`:
1. `setActivePopup({ username, kind, anchor })`.
2. Если `cache.has(username)` или `loadingUsernames.has(username)` — ничего не делает (popup сам отрендерит из cache или покажет loading).
3. Иначе — `loadingUsernames.add(username)` и стартует `signedGet('/api/users/username/' + username)`. На успех — `cache.set(username, data)`. На ошибку — `cache.set(username, { error: msg })`. В обоих случаях `loadingUsernames.delete(username)`.

`closeMention`: `setActivePopup(null)`. Кэш не очищается.

Provider рендерит `{children}` плюс `<MentionPopup />` если `activePopup !== null`.

### `MentionPopup.tsx`

Адаптивный — bottom-sheet на мобиле, anchored tooltip на desktop. Получает `activePopup` и cache из context'а через свой hook.

**Mobile (`<sm`):**
```tsx
<>
  <div className="fixed inset-0 z-40 bg-black/30 sm:hidden" onClick={closeMention} />
  <div className="fixed inset-x-2 bottom-2 z-50 ... sm:hidden">{content}</div>
</>
```

**Desktop (`sm+`):**
```tsx
<div
  className="hidden sm:block fixed z-50 w-[280px] ..."
  style={positionStyle /* {top|bottom, left} от anchor.getBoundingClientRect() */}
>
  {content}
</div>
```

Auto-flip: если `anchor.bottom + 260 > window.innerHeight` — popup идёт вверх (`top = anchor.top - 260`), иначе вниз (`top = anchor.bottom + 4`). `left = clamp(anchor.left, 8, window.innerWidth - 288)` — не вылезать за края.

Содержимое:
- Если `kind === 'role'` — header `Роль пользователя` (bold, primary color, small caps).
- Если `cache.has(username)` и не error — поля: имя (bold), `@username` (mono), email, role, отдел.
- Если error — текст ошибки (нейтральный).
- Если loading (нет в кэше, есть в loadingUsernames) — spinner / «Загрузка…».

Закрытие:
- Click на backdrop (mobile).
- Click вне popup'а (desktop) — useEffect с document.mousedown.
- Esc — useEffect с document.keydown.

### `MentionLink.tsx` — кнопка

Минимально:
```tsx
interface MentionLinkProps {
  username: string;
  kind?: 'user' | 'role';
  children: React.ReactNode;
}

export function MentionLink({ username, kind = 'user', children }: MentionLinkProps) {
  const { openMention } = useMentions();
  return (
    <button
      type="button"
      onClick={(e) => openMention(username, kind, e.currentTarget.getBoundingClientRect())}
      className="text-primary dark:text-primary-light hover:underline cursor-pointer"
    >
      {children}
    </button>
  );
}
```

Никакого state, никакого fetch'а. Чисто click-target.

## Engine-правка

`/root/rugpt/src/engine/routes/users.py` — `UserResponse` добавляет `department_name: Optional[str] = None`.

`UserStorage.get_by_username` (или соответствующий method) — JOIN с `departments` по `department_id`, возвращает `department_name` в результате. Если `department_id is None` — оставляем `None`.

Шаги:
1. Расширить `User` model (опционально `department_name: Optional[str]`).
2. SQL в `get_by_username`: `LEFT JOIN departments d ON d.id = u.department_id` + `d.name AS department_name`.
3. Routes — пробросить в `UserResponse`.

Безопасно: новое опциональное поле не ломает consumers.

## Использование в чате

В `MainChat` (и других страницах с MessageBubble) — обернуть chat container в `<MentionsProvider>`:

```tsx
<MentionsProvider>
  {/* messages list */}
</MentionsProvider>
```

В `MessageBubble.tsx`:
- `wireToDisplay` (string→string) **удаляется**.
- `MessageContent`:
  - **С references** (plain-text branch): `renderContentWithReferences` теперь возвращает ReactNode[]. Для каждого text-slice — applies regex `(^|\s)(@@?)(\w+)` напрямую и вставляет `<MentionLink>` ноды.
  - **Без references** (markdown branch): `<ReactMarkdown remarkPlugins={[remarkGfm, remarkMentions]} components={{ mention: MentionRenderer }}>{text}</ReactMarkdown>`. Без encoding'а, без href-хака — плагин делает всю работу.

## Network / data shape

**Endpoint:** `GET /api/users/username/{username}`.

**Response (после engine-правки):**
```ts
interface UserData {
  id: string;
  username: string;
  name: string;
  email: string;
  roleId: string | null;
  roleName: string | null;
  departmentId: string | null;
  departmentName: string | null;  // новое
  isAdmin: boolean;
  isHead: boolean;
}
```

**Auth:** `signedGet` из `apiClient` с user_id (как везде).

**Error handling:**
- 404 (username не найден) — popup показывает «Пользователь не найден».
- 5xx / network — «Не удалось загрузить инфо».

## Что вне scope V1

- Аватар в popup'е (поле `avatar_url` уже в response, добавить позже).
- Использование `MentionLink` на /tasks, /reports — компонент готов, миграция отдельным PR.
- `MentionLink` для cross-org user'ов (не существует на endpoint'е → 404 → ошибка).
- Persistent кэш между чатами / между сессиями. Только in-memory provider-кэш.

## Тесты

Frontend unit-тестов сейчас нет — manual E2E:
1. Сообщение `@anna привет` — `🧑anna` кликабельно, открывается popup с её данными.
2. Сообщение `@@anna эй` — `🤖anna` кликабельно, popup с header'ом «Роль пользователя».
3. Два разных mention'а в одном чате → клик на второй закрывает первый popup, открывает новый.
4. Один и тот же `@anna` упомянут трижды → fetch только при первом клике (видно в Network tab).
5. Mobile (320px) → bottom-sheet popup, не вылезает за края.
6. Desktop у нижнего края экрана → popup вверх (auto-flip).
7. 404 на несуществующего user'а → «Пользователь не найден».
8. Markdown-сообщение с `**жирным**` и `@anna` → markdown работает, mention кликабелен.
9. Сообщение с references (`!task-uuid`) и `@anna` → и pill, и mention кликабельны.
10. Esc / клик вне popup'а → закрывает.

Engine: добавить тест на `get_user_by_username` — возвращает `department_name` если есть department_id.

## Совместимость

- Engine: новое опциональное поле в response — не ломает.
- Webclient backend: shape расширяется опционально.
- Frontend: `wireToDisplay` (старая string→string функция) удаляется. Если она используется где-то вне MessageBubble — поправить (на момент спеки используется только там).

## Зависимости

`unist-util-visit` — обход AST в remark-плагине. Уже в transitive deps `react-markdown` (зависит от `unified`/`mdast-util-from-markdown`/`unist-util-visit`). Импортируем напрямую — должно резолвиться без дополнительной установки. Если нет — добавить в package.json.
