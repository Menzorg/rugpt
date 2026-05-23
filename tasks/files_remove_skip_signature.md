# Снять `@SkipSignature` с `/files/upload` и `/files/:id/download`

> Задача от тимлида: оба эндпоинта в WebClient помечены `@SkipSignature` —
> это легаси, остаток с момента когда подпись HMAC ещё не была включена
> для multipart/binary трафика. Сейчас остальной API подписан, исключение
> для файлов — дыра, через которую можно дёргать загрузку/скачку файлов
> без валидной подписи запроса.

**Цель.** Удалить декораторы `@SkipSignature` с двух эндпоинтов и убедиться,
что клиент (Next.js) подписывает соответствующие запросы корректно.

**Подход.** Минимальная правка в NestJS-прокси: убрать декоратор, прогнать
запросы через стандартный signature-guard. Параллельно — проверить и
поправить клиентский код, чтобы upload/download шли с подписью.

**Где код.** WebClient: `/home/wolflord/webclient_rugpt/` (NestJS + Next.js,
тонкий прокси к Engine). Engine (Python) не трогаем — он за подпись
не отвечает, валидация на NestJS-слое.

**Что НЕ входит:**
- Изменение схемы подписи (алгоритм, набор подписываемых полей) —
  legacy остаётся как есть, мы только перестаём пропускать file-роуты.
- Engine-сторона (`src/engine/routes/files.py`) — без изменений.
- Streaming-режим upload'а / прогресс-бар на клиенте — отдельная задача.

---

## Краткий обзор задач

| # | Задача | В двух словах | Статус |
|---|---|---|---|
| 1 | Найти декораторы и signature-guard | Где `@SkipSignature` определён, чем заменится при снятии. | ✅ done |
| 2 | Снять декоратор с двух роутов | Убрать `@SkipSignature` с `POST /files/upload` и `GET /files/:id/download`. | ✅ done |
| 3 | Проверить клиентскую подпись | Next.js клиент должен слать корректный signature header при upload/download. | ✅ done |
| 4 | Smoke + регрессия | Upload + download через клиент, прямой curl без подписи → 401. | ✅ done |
| 5 | Обновить JSDoc декоратора (целиком не удаляем) | Декоратор остаётся для auth/config/health/notification; на file он больше не нужен. | ✅ done |

---

## Структура файлов

| Файл (в `webclient_rugpt/`) | Роль |
|---|---|
| `packages/backend/src/file/file.controller.ts` | Снять `@SkipSignature` со строк 35 (`POST /files/upload`) и 56 (`GET /files/:id/download`) + почистить импорт (стр. 22) если станет неиспользуемым. |
| `packages/backend/src/common/guards/signature.guard.ts` | Декоратор `SkipSignature` определён прямо здесь (стр. 23–28). Guard читает подпись из `query` (GET) или `body` (остальные методы) на стр. 64–85. **Особой обработки multipart НЕТ** — это надо учесть в Task 3. |
| `frontend/.../upload.*` (Next.js client) | Гарантировать что request подписан перед отправкой. Точный путь — определяется на Subtask 3.1. |
| `frontend/.../download.*` | То же для GET-запроса скачки. Путь — на Subtask 3.2. |

Отдельного `skip-signature.decorator.ts` НЕТ — декоратор живёт в `signature.guard.ts`. Task 5 это учитывает.

---

## Архитектурные решения

- **Не менять алгоритм подписи.** Минимальный риск: только убираем
  исключение из общего правила, ничего нового не подписываем иначе.
- **Multipart upload подписываем по тем же полям, что и обычный JSON.**
  Не включаем body-бинарь в подпись (иначе хэш потока + retry-логика).
  Достаточно метода, пути, ts, nonce — как для остальных POST.
- **GET /download подписывается** теми же query-полями + path.
- **При неудаче клиента** — фронтенд должен показывать понятную ошибку,
  а не молчаливый 401. Если сейчас обработки 401 на upload/download нет —
  добавить.

---

## Task 1: Найти декораторы и signature-guard ✅ done

- [x] **1.1** `grep -rn "SkipSignature" /home/wolflord/webclient_rugpt/` —
  найти все вхождения: определение декоратора, guard, использования.
  - Декоратор определён в `signature.guard.ts:23-28` (НЕ в отдельном файле).
  - Используется в 5 контроллерах (9 роутов): auth (2), config (1),
    health (3), notification (1), file (2). На auth/config/health/notification
    декоратор останется — он нужен по дизайну.
- [x] **1.2** Открыть signature-guard, понять как он распознаёт
  «пропускать или нет» (метаданные через `Reflector` в NestJS).
  - Ключ метаданных: `SKIP_SIGNATURE_KEY = 'skipSignature'`.
  - Guard читает подпись из `request.query` (для GET) либо `request.body`
    (для POST/PUT/PATCH/DELETE). **Особой ветки для `multipart/form-data` НЕТ.**
  - Payload подписи: `${method}:${bodyStr}:${nonce}:${sigTimestamp}` —
    path не подписывается (известный недостаток схемы, вне scope).
  - **Важно для Task 3:** для upload'а (multipart) `request.body` после
    `multer` — это form-fields без файла. Подпись надо класть отдельными
    form-field'ами `signature/nonce/sig_timestamp/user_id`, чтобы guard её
    нашёл. Для download'а (GET) — обычные query params.
- [x] **1.3** Зафиксировать в этом файле точные пути файлов и имена
  методов контроллера (обновить таблицу выше). См. таблицу «Структура файлов».

---

## Task 2: Снять декоратор с двух роутов ✅ done

- [x] **2.1** Удалить `@SkipSignature()` над `POST /files/upload`.
- [x] **2.2** Удалить `@SkipSignature()` над `GET /files/:id/download`.
- [x] **2.3** Подчистить неиспользуемые импорты в файле контроллера —
  убран `import { SkipSignature } from '../common/guards/signature.guard'`.
- [x] **2.4** Прогнать `npm run lint` / `tsc --noEmit` — выполнено с оговоркой:
  - `npm run lint` падает на уровне конфига (ESLint 9 требует flat-config,
    в проекте старый `.eslintrc.*`). Pre-existing, не связано с правкой.
  - `npx tsc --noEmit` выдаёт 19 ошибок по отсутствующим модулям
    (`@webchat/common`, `nestjs-pino`, `pino`). Workspace `packages/common`
    не собран и часть зависимостей не установлена. **`file.controller.ts`
    в списке этих ошибок отсутствует** — наша правка типы не ломает.
  - Эти проблемы нужно описать в PR-description; чинить — отдельной задачей,
    вне scope нашего таска.

---

## Task 3: Проверить клиентскую подпись ✅ done

- [x] **3.1** Найдено: клиент слал 5 «сырых» `fetch` без подписи
  (upload/download), потому что подписывающий клиент `apiClient` не имел
  методов для multipart/binary.
- [x] **3.1a** Найдены все 5 точек: `useFiles.ts` (upload+download),
  `useAuthenticatedFile.ts` (хук blob + imperative download), `ChatInput.tsx` (upload).
- [x] **3.1b** В `transport/apiClient.ts` добавлены два подписывающих метода:
  - `signedGetBlob(path, userId?)` → `Promise<Blob>` — GET, подпись в query params,
    возвращает blob (для download и `<img src>`).
  - `signedPostFormData<T>(path, formData, userId?)` → `Promise<T>` — POST multipart.
    ⚠️ **Корректировка после теста (см. 3.4):** подпись кладётся в **query params**,
    а НЕ form-field'ами. Тело multipart не подписывается (`bodyStr=''`, как GET).
- [x] **3.1c** Все 5 точек переведены на новые методы:
  - `useFiles.ts:uploadFile` → `signedPostFormData`; `downloadFile` → `signedGetBlob`.
  - `useAuthenticatedFile.ts:useAuthenticatedFileBlob` → `signedGetBlob` (берёт userId из стора);
    `downloadFileWithAuth` — сигнатура сменена `token` → `userId?`, тело на `signedGetBlob`.
  - `MessageBubble.tsx` — вызов `downloadFileWithAuth` обновлён под новую сигнатуру (`currentUserId`).
  - `ChatInput.tsx:handleFilePick` → `signedPostFormData`; убраны неиспользуемые `BACKEND_URL`, `authToken`.
- [x] **3.3** Хелпер раньше не подписывал эти пути (их вообще обходили сырым fetch).
  Теперь подписывает. `tsc --noEmit` по фронту: новых ошибок типов нет; остаются только
  pre-existing `Cannot find module '@webchat/common'` (workspace не собран) — не связано с правкой.
- [x] **3.4** ⚠️ **Тест выявил баг в подходе 3.1b и потребовал правки guard'а (расширение scope).**
  - **Корень:** `SignatureGuard` зарегистрирован глобально (`APP_GUARD`, app.module.ts:99),
    а `FileInterceptor` (multer) — route-level интерсептор. В NestJS guard'ы выполняются
    ДО интерсепторов → на момент проверки подписи multipart-тело ещё не разобрано,
    `request.body` пуст. Подпись в form-field'ах guard НЕ видит → 401 на каждом upload.
  - **Фикс (frontend):** `signedPostFormData` теперь шлёт подпись в **query params**
    и подписывает пустое тело (`POST::nonce:ts`). `user_id` тоже в query — контроллер
    и так читает его через `@Query('user_id')`. Дубль `?user_id=` в `useFiles` убран.
  - **Фикс (backend, `signature.guard.ts`):** добавлена ветка для multipart —
    `isMultipart = content-type.includes('multipart/form-data')`, `fromQuery = GET || multipart`.
    Для multipart поля читаются из query, `bodyStr=''`. Криптосхема (алгоритм, поля) не тронута.
    JSON-роуты (POST/PUT/PATCH/DELETE) — без изменений (регрессии нет).
  - **Scope-замечание для тимлида:** «только снять декоратор» для upload недостаточно —
    guard физически не мог прочитать подпись multipart без этой правки.
  - **Побочная находка:** `department.controller.ts:@Post('org-context/upload')` — второй
    multipart-роут без `@SkipSignature`, страдал тем же багом (не работал для подписи).
    Правка guard'а теперь покрывает и его; его фронт-вызов — вне scope этой задачи.

---

## Task 4: Smoke + регрессия ✅ done

Прогон вживую: Engine (uvicorn :8100) + Postgres + NestJS backend (:4000),
тестовый юзер `rt_c` со сгенерённым device-ключом, JWT подписан секретом Engine.

- [x] **4.1** Подняты Engine + backend. Окружные блокеры (не наша правка):
  Engine — `tika` писал в `/tmp/tika.log` (root), лечится `TIKA_LOG_PATH`;
  backend — не собирался без `@webchat/common` (нужен `npm run build` в `packages/common`)
  и без `nestjs-pino`/`pino` (объявлены, но не установлены → `npm install`); pino-логгер
  писал в `/app/logs` (EACCES), лечится `LOGS_DIR`. **Всё — pre-existing setup, описать в PR.**
- [x] **4.2/4.3** Подписанный upload → **HTTP 201** (файл создан); подписанный download →
  **HTTP 200**, content-type `application/pdf`, байты идентичны загруженным (round-trip OK).
  Цепочка подтверждена целиком: подпись → guard (query, пустое тело) → реальная ECDSA-проверка в Engine.
- [x] **4.4** `curl -X POST -F file=@test.pdf .../api/files/upload` БЕЗ подписи →
  **HTTP 401 `"Missing signature data"`** (раньше было бы 200 из-за `@SkipSignature`). ✅ дыра закрыта.
- [x] **4.5** Негативные пробы: поддельная подпись → 401 `"Invalid signature"`;
  протухший timestamp (-400s) → 401 `"Request timestamp expired"`;
  валидная подпись без JWT → 401 (JwtAuthGuard срабатывает после SignatureGuard — порядок подтверждён).
- [x] **4.6** Команды/ответы — в этом разделе; для PR перенести вместе с окружными оговорками (4.1).

---

## Task 5: Обновить JSDoc декоратора (не удаляем) ✅ done

- [x] **5.1** Повторный `grep` подтвердил: декоратор используется в auth/config/health/notification.
- [x] **5.2** НЕ удаляем — он нужен по дизайну для публичных эндпоинтов.
- [x] **5.3** В JSDoc `SkipSignature` (signature.guard.ts) добавлено ⚠️-предупреждение:
  использовать только для заведомо публичных роутов, каждое новое применение — через ревью;
  отдельно отмечено, что file-роуты намеренно его НЕ используют (multipart подписывается через query).

---

## Self-review

**Покрытие исходного запроса:**
- ✅ Снять `@SkipSignature` с `/files/upload` и `/files/:id/download` → Task 2.
- ✅ Убедиться что после снятия всё работает → Task 3 + Task 4.
- ✅ Не оставлять legacy-определение без надобности → Task 5.

**Возможные риски:**
- Клиентский upload использует multipart с прогресс-бара или
  resume-логику — может не дружить с подписью, если та зависит от
  body. Минимизируется тем, что подпись body не покрывает (см.
  «Архитектурные решения»). Проверить на Task 3.
- Внешние интеграции (если кто-то дёргал `/files/upload` напрямую
  как webhook) — сломаются. Уточнить у тимлида: есть ли такие?
  Если есть — отдельная коммуникация с пользователями API.
- Тесты: если в `webclient_rugpt` есть e2e, они могли полагаться на
  отсутствие подписи на этих роутах. На Task 4 прогнать существующий
  test suite.

---

## Рекомендуемый порядок

1. **Task 1** — без правок, только разведка. Зафиксировать пути файлов.
2. **Task 2** — собственно снять декораторы. Маленький коммит.
3. **Task 3** — проверить клиент, поправить если нужно.
4. **Task 4** — smoke, доказательства, PR.
5. **Task 5** — чистка после PR, отдельным коммитом если получится.
