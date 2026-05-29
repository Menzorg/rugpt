# Zero Trust: перенос enforcement подписей в движок

**Дата:** 2026-05-22
**Статус:** дизайн утверждён, ожидает ревью спека → план
**Затрагивает:** `rugpt` (движок), `webclient_rugpt` (NestJS backend + Next.js frontend + packages/common), nginx на rugpt-container

## Проблема

Текущая Zero Trust в rugpt **не выполняет свою цель** — «не доверять машине вебклиента, чтобы при её компрометации нельзя было исказить запросы». Установлено по коду (доки утверждают обратное):

1. **Бизнес-роуты движка подпись не проверяют.** Проверка живёт только в отдельном `POST /auth/verify-signature`, который зовёт NestJS `SignatureGuard`. При успехе гард вырезает signature-поля и форвардит мутацию в движок уже без подписи → движок не связывает проверенную подпись с реальной мутацией.
2. **Весь enforcement в NestJS** — на узле, который по модели угроз недоверенный. Скомпрометированный NestJS просто не зовёт verify-signature и бьёт по бизнес-роуту движка напрямую.
3. **`send_message` даже JWT не валидирует**: `user_id: UUID, # In real app, get from JWT` — берёт `user_id`/`org_id` из параметров и верит на слово.
4. **Path/route не подписан** (`payload = method:body:nonce:timestamp`), **nonce-replay не проверяет никто**, timestamp ±5мин проверяет только NestJS.

Эталон правильной реализации — `rptext`: `src/engine/middleware/web_signature.py` (`WebSignatureMiddleware`, подключён в `app.py`), проверка подписи на самом движке против device-ключей из БД, nonce в Redis (`SET NX EX 300`), timestamp на движке.

## Граница и цель (scope)

Закрываем **угрозу A** — скомпрометированный backend/прокся NestJS не может:
- подделать запрос (нет приватного device-ключа),
- реплейнуть (одноразовый nonce на движке),
- эскалировать действие (route-id в подписи),
- подменить личность/тенанта (`user_id` проверен подписью, `org_id` выводится серверно).

**Вне scope — угроза B** (скомпрометированная *доставка* фронта). Фронт раздаётся с того же VPS A, что и NestJS; если атакующий контролирует отдаваемый в браузер JS, он подписывает произвольные запросы ключом жертвы (ключ non-extractable, но usage `sign` доступен коду в origin'е). Подпись запросов этого в принципе не закрывает. Целостность доставки фронта — **отдельная граница доверия** (отдельный хостинг/SRI/иммутабельные ассеты), разбирается отдельной работой. Здесь только фиксируем границу.

## Утверждённые решения

| # | Решение | Выбор |
|---|---|---|
| 1 | Где enforcement | Middleware на движке (порт `WebSignatureMiddleware` из rptext) |
| 2 | Привязка личности | Подпись = аутентификация; `user_id` проверяется против device-ключей этого юзера (модель rptext) |
| 3 | `org_id` | Выводится серверно из `user.org_id`, **не принимается параметром** |
| 4 | Nonce-replay | Redis на движке (`SET nonce:{user_id}:{nonce} NX EX 300`) |
| 5 | Timestamp | ±300с, проверяется на движке |
| 6 | Привязка пути | **route-id (уровень действия)**: `payload = METHOD:route_id:body:nonce:ts`. Закрывает эскалацию действия. Остаток (подмена ресурса при том же действии) принят |
| 7 | Публичные роуты | Минимум: login, engine-public-key, config, health, telegram/webhook. Все прочие текущие дыры закрываем |
| 8 | Вход/маршрутизация | `/api/v1/web/*`; nginx движка **проксирует `/web` нетронутым**, rewrite `/web`→`/api/v1` делает middleware после проверки |
| 9 | Rollout | Координированный деплой без флага; юзеры перелогинятся |

## Архитектура

### Поток подписанного запроса (целевой)

```
Браузер (apiClient):
  route_id = REGISTRY[action]            // из packages/common
  nonce = random16hex, ts = now()
  clean_body = recursiveSort({...data})  // канонизация (см. ниже)
  payload = `${METHOD}:${route_id}:${JSON.stringify(clean_body)}:${nonce}:${ts}`
  signature = ECDSA_sign(payload, devicePrivateKey)   // ключ из IndexedDB
  → запрос на NestJS, поля signature/nonce/sig_timestamp/user_id/route_id в body (GET — в query)
        │
NestJS (прозрачное реле для подписи):
  - НЕ проверяет подпись (это делает движок)
  - НЕ реформатит подписанное body
  - форвардит signature/nonce/sig_timestamp/user_id/route_id в движок нетронутыми
  - адаптер шлёт на /api/v1/web/<engine-suffix>
        │
nginx (rugpt-container, 10.0.0.2:80):
  - проксирует /api/v1/web/* на FastAPI БЕЗ среза /web
        │
Движок WebSignatureMiddleware (перехват /api/v1/web/*):
  1. allowlist (WEB_ALLOWED_ROUTES) — иначе 404
  2. no-signature? (WEB_NO_SIGNATURE_ROUTES) — да → форвард без проверки
  3. извлечь signature-поля (GET: query, иначе: body)
  4. check_timestamp(±300)
  5. собрать payload из ФАКТИЧЕСКИХ method+route_id+clean_body+nonce+ts
  6. verify_request_signature: перебор device-ключей user_id → ECDSA verify
  7. валидация route_id ↔ фактический эндпоинт (map route_id→(method,path-pattern))
  8. check_and_store_nonce (Redis SET NX EX 300) — replay
  9. форвард: rewrite /web→/api/v1, strip signature-полей + route_id
        │
Бизнес-роут движка:
  - user_id доверенный (проверен подписью)
  - org_id выводится из user.org_id (не из param)
```

### Канонизация payload (единый контракт)

Здесь была дыра rugpt (фронт сортировал рекурсивно, NestJS — только верхний уровень → рассинхрон для вложенных тел). Фиксируем единый формат:

- **Python (движок):** `json.dumps(clean_body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)`
- **TS (фронт):** `JSON.stringify(sortKeysRecursively(clean_body))` (без пробелов, рекурсивная сортировка, unicode сохраняется)
- `clean_body` = тело без полей `{signature, nonce, sig_timestamp}` (но **с** `user_id`).
- GET: `clean_body` пустой (`""`).
- Обязателен тест парности TS↔Python на формат payload.

## Компоненты и изменения

### Движок (`/root/rugpt/src/engine/`)

- **`middleware/web_signature.py`** (новый) — `WebSignatureMiddleware`, порт из rptext с адаптацией под route-id и derive-org_id.
- **`services/crypto_service.py`** — есть `verify_device_signature`. Добавить `verify_request_signature(user_id, payload, signature, nonce, timestamp)` = timestamp + nonce(Redis) + перебор device-ключей. (rptext держит это в CryptoService-миксине с `self.storage.redis`; в rugpt crypto — модуль функций, поэтому Redis-клиент и сборка проверки оформляются явно.)
- **Redis** — поднять на движке (переменные в `config.py` есть, не используются), клиент в `EngineService`, использование только для nonce.
- **`config.py`** — `WEB_ALLOWED_ROUTES`, `WEB_NO_SIGNATURE_ROUTES`, активировать Redis-конфиг.
- **`app.py`** — `add_middleware(WebSignatureMiddleware)`.
- **`routes/auth.py`** — удалить `POST /auth/verify-signature` (больше не нужен).
- **Бизнес-роуты** — перестать принимать `org_id` параметром; выводить из `user.org_id`. `user_id` остаётся, но теперь доверенный. Затрагивает `send_message` и аналогичные роуты с `user_id/org_id` в сигнатуре.
- **Реестр route-id (Python)** — map `route_id → (method, path-pattern)` для валидации шага 7.

### nginx (rugpt-container)

- Сменить текущий rewrite `^/api/v1/web/(.*)$ → /api/v1/$1` на **чистый proxy** `/api/v1/web/*` → FastAPI без среза префикса. Rewrite переезжает в middleware.

### NestJS (`/root/webclient_rugpt/packages/backend/`)

- **`common/guards/signature.guard.ts`** — убрать вызов verify в движок (движок сам проверяет). Гард удалить целиком либо свести к no-op. Логика retry/503 при транспортных сбоях verify-вызова больше не нужна (нет отдельного вызова).
- **`engine/adapters/rugpt.adapter.ts`** — форвардить signature/nonce/sig_timestamp/user_id/route_id в движок нетронутыми; слать на `/api/v1/web/*`; **не трансформировать подписанное body**. Проверить, где адаптер сейчас реформатит тело (camelCase↔snake_case) для подписанных роутов — такие трансформации должны уехать на фронт (до подписи) либо быть исключены.
- **`@SkipSignature`** — теряет смысл (enforcement на движке); публичный набор живёт в `WEB_NO_SIGNATURE_ROUTES` движка. Декораторы удалить вместе с гардом.

### Frontend (`/root/webclient_rugpt/packages/frontend/`)

- **`transport/apiClient.ts`** + **`utils/crypto.ts`** — `createSignaturePayload(method, route_id, body, nonce, ts)` → `${method}:${route_id}:${body}:${nonce}:${ts}`; каждый signed-вызов передаёт route_id из реестра.
- Подписать ранее неподписанные роуты: files upload/download, folders GET, invoices POST, org-context upload.
- **Канонизация** — привести к единому контракту (рекурсивная сортировка уже есть; убедиться в парности с Python).

### packages/common

- **Реестр route-id** (constants) — единый источник правды, используется фронтом (подписать) и форвардится через NestJS. Зеркалится/генерируется для Python-стороны движка.

## Публичные роуты

**No-signature (`WEB_NO_SIGNATURE_ROUTES`):**
- `/auth/login` (ключа ещё нет)
- `/auth/engine-public-key` (нужен до логина — шифрование пароля RSA-OAEP)
- `/config` (maintenance-статус)
- `/health*`
- `/notifications/telegram/webhook` (server-to-server)

**Закрываем (теперь под подписью):** `/files/upload`, `/files/{id}/download`, `/folders` (GET список/tree/`{id}`/`{id}/files`), `/invoices` (POST), `/departments/org-context/upload`.

**Регистрация** в rugpt отдельной публичной саморегистрации не имеет — пользователей создаёт админ через подписанный `POST /users`.

## Открытые детали для плана

Это не пробелы дизайна, а решения, делегированные этапу плана:

1. **Синхронизация реестра route-id TS↔Python** — кодген (как `webclient_engine_adapter_generator` в rptext) vs ручной парный список. Выбрать механизм против дрейфа.
2. **Подпись multipart** (files upload/download) — тело не JSON. Подписывать `route_id+nonce+ts` + **SHA-256 содержимого файла** в payload (иначе скомпрометированная прокся подменит файл). Детализировать схему.
3. **Трансформация тела в NestJS** — найти все места, где адаптер реформатит body подписанных запросов, и решить судьбу каждого (унести на фронт до подписи / исключить).
4. **Список бизнес-роутов с `org_id` в параметрах** — полный перечень для перевода на derive-from-user.
5. **Точная nginx-конфигурация** на rugpt-container (proxy без rewrite) — свериться с фактическим конфигом на проде (не по докам).

## Тестирование

- **Движок (middleware):** валидная подпись проходит; битая подпись → 401; replay nonce → 401; протухший timestamp → 401; чужой route_id → 401; запрещённый роут → 404; no-signature роут проходит без подписи; `org_id` не принимается из param.
- **Парность канонизации** TS↔Python — отдельный тест на формат payload (регресс на старую дыру shallow-sort).
- **Идемпотентность nonce** — конкурентные одинаковые nonce: ровно один проходит.

## Остаточные риски (приняты явно)

- **Подмена ресурса при том же действии** (route-id уровень действия, не литеральный path): скомпрометированная прокся может перенаправить «жертва удаляет чат A» → «удаляет чат B» — в рамках прав жертвы. Принято как мягкий компромисс против стоимости литерального path.
- **Угроза B** (доставка фронта) — вне scope, отдельная граница доверия.
- **Nonce в Redis на движке** — переживает рестарт процесса (в отличие от in-memory), но Redis становится новой зависимостью движка. При недоступности Redis — определить поведение (rptext: fail-open «лучше работать чем падать»; для rugpt решить на этапе плана, fail-open vs fail-closed).

## Архитектурная правка (as-built)

Этот раздел фиксирует, чем фактическая реализация (План A на dev) отличается от дизайна выше. Тело спека не переписывается — правки точечные.

- **Проверка вынесена в композитный сервис.** Вместо того чтобы middleware напрямую импортировал crypto-хелперы и Redis-клиент (как предполагалось в разделе «Компоненты»), вся логика проверки собрана в метод `SignatureService.verify_request_signature` (`src/engine/services/signature_service.py`, встроен в `EngineService` как `engine.signature_service`). Схема — route/middleware → service → storage: middleware делает один делегирующий вызов и сам crypto/storage не трогает (ловит только `NonceStoreUnavailable` → 503). Внутри сервиса: timestamp-окно → device-ключи юзера → ECDSA verify → nonce-replay (`NonceStore`, fail-closed).
- **Открытый вопрос «синхронизация реестра route-id» снят.** Отдельного Python-реестра route-id нет. `route_id` — это шаблон роута самого движка, резолвится через `resolve_route_template` против собственного роут-тейбла приложения (`app.routes`). Поддерживать в синхроне нечего; фронт зеркалит те же шаблоны, парность покрывается Планом C.
