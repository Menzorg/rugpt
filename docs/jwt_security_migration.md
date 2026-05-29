# Миграция: JWT уходит из Engine, identity = ECDSA-подпись

> Адресат: разработчик, который будет реализовывать миграцию. Документ описывает что есть сейчас, почему так нельзя, что делаем и в каком порядке.

## Цель

Перевести Engine на модель Zero Trust: identity пользователя для авторизации действия Engine берёт **только** из ECDSA-подписи устройства, а не из JWT. JWT остаётся внутренним механизмом webclient для UX-сессии (frontend ↔ NestJS) и до Engine не доходит.

После миграции:
- Engine не имеет `JWT_SECRET`, не парсит `Authorization` header.
- Engine не доверяет ни NestJS, ни VPN — каждый mutation проверяется ECDSA.
- `JWT_SECRET` живёт только в NestJS, его утечка не даёт подделать identity на Engine.
- `POST /auth/verify-signature` исчезает, потому что NestJS больше не оракул для подписей — Engine проверяет сам.

## TL;DR

| Слой | Сейчас | После миграции |
|---|---|---|
| Frontend → NestJS | JWT (Authorization) + ECDSA подпись (body/query) | без изменений |
| NestJS → Engine | JWT (Authorization), часто ещё `user_id` query | только ECDSA подпись (без JWT) |
| Engine identity | `payload["user_id"]` из JWT | `signed_payload.user_id`, проверенный ECDSA |
| Engine verify | внешний `POST /auth/verify-signature` для NestJS | внутренний `Depends(get_signed_user)` на каждом mutation |
| `JWT_SECRET` | общий между Engine и NestJS | только в NestJS |
| Nonce / timestamp | проверяет только NestJS | проверяет только Engine |
| WebSocket | JWT на handshake | без изменений (WS живёт между frontend и NestJS, до Engine не доходит) |

## 1. Текущее состояние

### 1.1. Конвейер запроса

```
Frontend (Next.js)
  apiClient.signedPost(path, body, currentUser?.id)
    bodyForSignature = { ...data, user_id: userId }
    sortedBody       = recursiveSortKeys(bodyForSignature)
    payload          = `${method}:${JSON.stringify(sortedBody)}:${nonce}:${timestamp}`
    signature        = ECDSA.sign(payload, privateKey)        # privkey non-extractable, IndexedDB
    body            ← { ...data, user_id, signature, nonce, sig_timestamp }
  httpClient: + Authorization: Bearer <JWT>                    # JWT из useAuthStore (localStorage)
  ↓
NestJS (global guards)
  SignatureGuard:
    извлекает userId, signature, nonce, sig_timestamp из body/query
    реконструирует payload (user_id остаётся в cleanBody)
    timestamp ±300s (локально)
    POST → Engine /auth/verify-signature {user_id, payload, signature}    # Engine — оракул
    при success: удаляет signature/nonce/sig_timestamp из body, user_id оставляет
    в req.user НЕ пишет ничего
  JwtAuthGuard / JwtStrategy:
    декодирует JWT (общий JWT_SECRET с Engine)
    req.user = { id: payload.user_id, orgId, isAdmin, engineToken }       # identity из JWT
  ↓
NestJS controller:
  task.controller.ts        → user_id берёт из req.user (JWT)
  chat.controller.ts:76-94  → user_id берёт из @Query('userId') (URL!)
  остальные                 → смесь
  ↓
RuGPTEngineAdapter:
  Authorization: Bearer <engineToken>    # тот же JWT, форвардится
  на части эндпоинтов: ?user_id=...&org_id=... в URL/query
  на других: только JWT, Engine достаёт user_id сам
  ↓
Engine (FastAPI)
  большинство роутов: Depends(get_current_user)
    JWT.decode(token, JWT_SECRET, HS256)
    user_id = UUID(payload["user_id"])    # identity из JWT
  chats.py: user_id: UUID = Query(...)   # identity из URL, без auth-dependency
  /auth/verify-signature: принимает {user_id, payload, signature}
    БЕЗ парсинга payload, БЕЗ nonce-cache, БЕЗ timestamp check
    проверяет ECDSA против opaque-строки, возвращает {valid: bool}
```

### 1.2. Где Engine достаёт identity (карта)

`src/engine/routes/auth.py`:
- `auth.py:23` — `security = HTTPBearer(auto_error=False)`
- `auth.py:74-84` — `create_token(user_id, org_id, email, is_admin)` кладёт `user_id` (не `sub`) в JWT payload
- `auth.py:87-95` — `verify_token` декодирует HS256 с `Config.JWT_SECRET`
- `auth.py:98-125` — `get_current_user` — основной FastAPI dependency, возвращает `{user_id, org_id, is_admin, department_id, is_head}` из JWT + БД-проверка `is_active`
- `auth.py:262-293` — `POST /auth/verify-signature`: принимает `{user_id, payload, signature}`, без JWT, без парсинга payload, без nonce, возвращает `{valid: bool}`

`src/engine/services/crypto_service.py:12-60` — `verify_device_signature(public_key_pem, payload, signature)`. ECDSA P-256 + SHA-256, принимает IEEE P1363 (64 байта) и DER. Единственный потребитель — `auth.py:288`. Бизнес-роуты не вызывают.

`src/engine/routes/chats.py` — все mutation-роуты (chats.py:128, 141, 154, 174, 226, 294, 338, 431, 462, 523, 573) берут `user_id` из `Query(...)` без `Depends(get_current_user)`. Комментарий `chats.py:469-472`: «Identity берётся из подписанного payload (Zero Trust...) не из JWT» — но Engine сам подпись на этих запросах никогда не проверяет.

`src/engine/app.py:70-77` — middleware: только `RequestLoggingMiddleware`, `CorrelationIDMiddleware`, `CORSMiddleware`. Никакого global auth enforcement.

Прочие routes (`tasks.py`, `users.py`, `files.py`, `notifications.py`, ...) используют `Depends(get_current_user)` — identity из JWT.

### 1.3. NestJS

`packages/backend/src/common/guards/signature.guard.ts`:
- Зарегистрирован глобально (`app.module.ts:98-101`).
- WS пропускает (`signature.guard.ts:51`).
- POST/PATCH: `userId = request.body?.user_id` (line 81).
- GET: `userId = request.query?.user_id` (line 73-79).
- Реконструирует payload `${method}:${JSON.stringify(cleanBody, sorted_keys)}:${nonce}:${sig_timestamp}` (line 99-117).
- Timestamp tolerance 300s (line 13, 93-96).
- `engineAdapter.verifySignature({user_id, payload, signature})` (line 125-129) → `Engine /auth/verify-signature`.
- На успехе удаляет `signature/nonce/sig_timestamp` из body, **`user_id` оставляет** (line 144-146). В `req.user` не пишет.

`packages/backend/src/auth/jwt.strategy.ts:24-43`:
- Декодирует JWT тем же `JWT_SECRET`, что и Engine.
- `req.user = { id: payload.user_id, orgId, isAdmin, engineToken }` — identity из JWT.

`packages/backend/src/engine/adapters/rugpt.adapter.ts`:
- Все запросы в Engine идут с `Authorization: Bearer <engineToken>` (line 210-214).
- На части эндпоинтов дополнительно прокидывает `user_id` в URL/query/body (`send_message`, `validate_ai_message`, `reject_ai_message`, etc.) — берёт из `payload.user_id`, который controller передал из `req.user` или `@Query('userId')`.
- На остальных (`create_task`, `take_task`, `get_in_app_notifications`, etc.) — только JWT.

`SkipSignature` эндпоинты:
- `auth.controller.ts:18,27` (`GET /auth/engine-public-key`, `POST /auth/login`)
- `health.controller.ts:54,66,75`
- `file.controller.ts:35,56` (upload, download)
- `notification.controller.ts:26` (telegram webhook)
- `config.controller.ts:23`

### 1.4. Frontend

`packages/frontend/src/utils/crypto.ts`:
- `generateDeviceKeys` (line 90-115) — Web Crypto `ECDSA P-256`, приватный ключ re-imported с `extractable=false`.
- `signPayload(payload, privateKey)` (line 130-146) — ECDSA + SHA-256, base64.
- `createSignaturePayload(method, body, nonce, timestamp)` (line 165-167) — `${method}:${bodyStr}:${nonce}:${timestamp}`. **Path не включается** (комментарий: "frontend подписывает путь к NestJS, Engine получает другой").

`packages/frontend/src/utils/deviceKeys.ts`:
- IndexedDB `zero-trust-keys` / store `device-keys` / key `current` (line 13-15).
- Ключи "не удаляются при logout" (line 8).

`packages/frontend/src/transport/apiClient.ts:115-150` — `addSignature`:
- `bodyForSignature = { ...data, user_id: userId }` (line 130) — userId приходит **аргументом** от вызывающего React-кода.
- Подписанный body содержит: `user_id`, `signature`, `nonce`, `sig_timestamp`.
- POST/PUT/PATCH/DELETE: подпись в JSON body (line 159-206).
- GET: подпись в query string (line 212-235).

`packages/frontend/src/app/hooks/useAuth.ts:38-147` — Zustand store `useAuthStore` с `persist` в localStorage (key `auth-storage`). `user.id` пишется из ответа `/api/auth/login` — то, что бэкенд сообщил клиенту, в plaintext localStorage, **не подписано клиентом**.

## 2. Проблемы текущей схемы

### 2.1. Identity берётся из JWT (главная проблема)

Engine на 95% роутов — `Depends(get_current_user)` → JWT.decode → `payload["user_id"]`. Подпись Engine никогда не видел: её проверял NestJS upstream через `/auth/verify-signature`, и **связи между verified user_id и user_id, который попадёт в Engine, нет**.

Конкретный сценарий атаки: имея валидный `JWT_SECRET` (один общий на Engine + NestJS, security-пункт 2 в `tech-debt.md`), атакующий выпускает JWT с произвольным `user_id = X`, при этом подписывает запрос **своим** устройством с `user_id = свой`. SignatureGuard проверяет — OK, ECDSA валидна. JwtAuthGuard ставит `req.user.id = X`. Engine на task/file/users роутах действует от имени X.

### 2.2. /auth/verify-signature без auth, без nonce, без timestamp

`auth.py:269-293` — публичный эндпоинт без `Depends(get_current_user)`, без rate-limit. Принимает opaque `payload: str` и не парсит его — то есть Engine не может проверить, что подпись соответствует конкретному HTTP-запросу. Подпись на `POST /messages` body=B валидна для любого `POST` с body=B. Nonce и timestamp Engine не проверяет. Если Engine когда-нибудь попадёт под прямой вызов в обход NestJS, replay 24/7.

### 2.3. chats.py обходит auth целиком

Все mutation-роуты в `chats.py` берут `user_id` из query без `Depends(get_current_user)` и без проверки подписи самим Engine. Если кто-то достучится до `127.0.0.1:8100` (через 10.0.0.0/24 или RCE на rugpt-container), `POST /chats/{id}/messages?user_id=<любой>` пишет от любого имени.

Комментарий в `chats.py:469-472` обещает: «Identity берётся из подписанного payload, не из JWT» — но Engine подпись на этом запросе не проверяет. Это полная фикция.

### 2.4. JWT_SECRET общий между Engine и NestJS

`security-пункт 2` в `tech-debt.md`. Утечка секрета на любом из двух сервисов = подделка identity на обоих.

### 2.5. Path не подписан

`crypto.ts:165-167` явно исключает path: «frontend подписывает путь к NestJS, Engine получает другой». Подпись на `POST /messages` валидна на `POST /tasks` с тем же body+nonce+ts.

### 2.6. user_id в подписи — что захочет, то и положит

`apiClient.signedPost(path, body, userId)` — третий аргумент. Вызывающий React-код берёт его из `useAuthStore.user.id` (plaintext localStorage). В DevTools поменять — тривиально. SignatureGuard проверяет подпись против ключей **указанного** user_id; если у атакующего ключ не от X, подпись для X не пройдёт. Но логически: identity у клиента — необязательно та, что в JWT.

### 2.7. NestJS не сверяет JWT.user_id и signed_payload.user_id

Грэп по `!==` / `!=` для user_id в `packages/backend/src/` — пусто. SignatureGuard и JwtAuthGuard работают в параллель и про друг друга не знают.

### 2.8. Прочее (мелочи, попадающие под чистку)

- Nonce-cache не существует (грэп по `nonce` в `src/engine/` — одна строка docstring).
- WS auth — только JWT (`signature.guard.ts:51` пропускает не-HTTP). Это остаётся как есть, но фиксируется.
- Legacy `SkipSignature` для file upload/download — security-пункт 9 в `tech-debt.md`. Решается отдельно, но в этой миграции тоже стоит привести в порядок.

## 3. Целевая архитектура

### 3.1. Схема

```
Frontend (Next.js)              NestJS (proxy + UX session)        Engine (источник правды)
─────────────────              ──────────────────────────         ─────────────────────────
JWT (NestJS-signed)             JWT_SECRET (только здесь)         нет JWT_SECRET
ECDSA privkey (IndexedDB)       SignatureGuard упрощён            get_signed_user dependency
useAuthStore.user (UX-only)     Engine adapter без Authorization  nonce_cache + timestamp
                                                                  device revoke endpoint

Frontend ──JWT + ECDSA──> NestJS ──ECDSA only──> Engine
   (UX session)             (UX session)         (identity)
```

### 3.2. Роли компонентов

**Frontend.** Хранит JWT (выданный NestJS) в localStorage для UX (отрисовать "залогинен/нет", logout). Хранит приватный ключ устройства в IndexedDB non-extractable. На каждом mutation добавляет ECDSA-подпись body. JWT не виден Engine.

**NestJS.** Свой `JWT_SECRET`, не общий с Engine. Выпускает JWT на login после успешной проверки на Engine. Глобальный SignatureGuard остаётся для UX-цели (быстрый отказ невалидных подписей до проксирования), но **больше не оракулит Engine** — проверяет подпись локально (если нужно) или просто прокидывает signed body как есть. JwtAuthGuard остаётся для своих контроллеров. Сверяет `req.user.id == signed.user_id`. Engine adapter не прокидывает `Authorization`. WS handshake — JWT как сейчас.

**Engine.** Не знает про JWT. На каждом mutation `Depends(get_signed_user)` парсит signature/nonce/sig_timestamp/user_id из request, проверяет ECDSA, timestamp ±5min, nonce uniqueness. Identity = `signed.user_id`. `org_id`, `is_admin` фетчатся из БД по user_id. `/auth/login` возвращает данные юзера без JWT. `/auth/verify-signature` удалён. Новый `/auth/logout` revoke'ит device.

### 3.3. Жизненный цикл mutation-запроса

```
1. Пользователь жмёт кнопку в UI.
2. apiClient.signedPost(path, body, userId):
   - bodyForSignature = { ...body, user_id: userId, path }    # path добавляется!
   - payload = `${method}:${JSON.stringify(sortedBody)}:${nonce}:${ts}`
   - signature = ECDSA.sign(payload, privKey)
   - http.post(path, { ...body, user_id, signature, nonce, sig_timestamp, path })
   - headers: Authorization: Bearer <NestJS JWT>
3. NestJS SignatureGuard:
   - извлекает signature/nonce/sig_timestamp/user_id/path
   - timestamp ±300s
   - (опционально) локальная ECDSA проверка через Engine pubkey-cache  
     ИЛИ просто пропуск дальше, Engine всё равно проверит
   - удаляет служебные поля (или оставляет, всё равно прокинутся)
4. NestJS JwtAuthGuard:
   - req.user = { id: payload.user_id, ... }
5. NestJS свой guard: assert req.user.id == body.user_id (или query.user_id) → 401 если расходится
6. NestJS controller → service → engineAdapter.execute(...):
   - НЕ кладёт Authorization
   - Прокидывает signed body как есть в Engine /api/v1/<route>
7. Engine route с Depends(get_signed_user):
   - читает body/query
   - проверяет timestamp
   - проверяет nonce в signature_nonces (INSERT, unique constraint → 401 если duplicate)
   - lookup public keys для signed_payload.user_id в user_devices
   - verify ECDSA против реконструированного payload (включая path)
   - lookup user в БД (is_active, org_id, is_admin, ...)
   - возвращает SignedUser dataclass
8. Бизнес-логика использует SignedUser.user_id как authoritative.
```

### 3.4. Login / logout

**Login:**
```
Frontend                      NestJS                         Engine
   │                            │                              │
   │  POST /api/auth/login      │                              │
   │  { email, password,        │                              │
   │    device_pubkey, dev_name}│                              │
   ├───────────────────────────>│                              │
   │                            │  POST /api/v1/auth/login     │
   │                            │  (без подписи: bootstrap)    │
   │                            │  { email, password,          │
   │                            │    device_pubkey, dev_name } │
   │                            ├─────────────────────────────>│
   │                            │                              │ verify password
   │                            │                              │ register device key
   │                            │                              │ (или обновить existing)
   │                            │  { user_id, org_id, is_admin,│
   │                            │    name, username,           │
   │                            │    device_id }               │
   │                            │<─────────────────────────────┤
   │                            │                              │
   │                            │ NestJS выпускает свой JWT    │
   │                            │ JwtService.sign({user_id,    │
   │                            │   org_id, is_admin, exp})    │
   │  { token, user, device_id }│ своим секретом (NESTJS_JWT_  │
   │<───────────────────────────┤  SECRET, не равен ENGINE     │
   │                            │  ничего)                     │
   │ store in localStorage      │                              │
```

**Logout:**
```
Frontend                      NestJS                         Engine
   │ POST /api/auth/logout      │                              │
   ├───────────────────────────>│                              │
   │                            │ DELETE /api/v1/auth/devices/ │
   │                            │   {device_id}                │
   │                            │ (signed mutation, как любой) │
   │                            ├─────────────────────────────>│
   │                            │                              │ revoke device
   │                            │                              │ (set revoked_at)
   │                            │<─────────────────────────────┤
   │ wipe localStorage          │ (опционально) blacklist JWT  │
   │ wipe IndexedDB (?)         │                              │
   │<───────────────────────────┤                              │
```

Открытый вопрос: удалять ли приватный ключ из IndexedDB при logout. Аргумент за: чистый logout, следующий логин = новое устройство. Аргумент против: пользователь хочет "переключиться", не теряя trusted device. Решение — оставить ключ, при revoke device на Engine все запросы будут падать с 401, при следующем логине Engine увидит уже зарегистрированный pubkey и **переактивирует** device (новый `revoked_at = NULL`). Это пишется отдельно.

## 4. Что меняется в Engine

### 4.1. Новая миграция БД

`src/engine/migrations/019_signed_auth.sql`:

```sql
-- Nonce cache (replay protection)
CREATE TABLE signature_nonces (
    nonce TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ix_signature_nonces_expires ON signature_nonces(expires_at);

-- Device revoke + tracking
ALTER TABLE user_devices
    ADD COLUMN revoked_at TIMESTAMPTZ,
    ADD COLUMN last_used_at TIMESTAMPTZ,
    ADD COLUMN expires_at TIMESTAMPTZ;
```

`expires_at` — опционально (например `NOW() + INTERVAL '90 days'`). Если не нужен принудительный re-pairing, можно оставить NULL.

Cleanup просроченных nonce'ов — через scheduler job (например в `SchedulerService` каждый час `DELETE FROM signature_nonces WHERE expires_at < NOW()`).

### 4.2. Новый dependency `get_signed_user`

`src/engine/dependencies/auth.py` (новый файл):

```python
from dataclasses import dataclass
from uuid import UUID
from fastapi import Request, HTTPException, Depends
from ..services.engine_service import get_engine_service

@dataclass
class SignedUser:
    user_id: UUID
    org_id: UUID
    is_admin: bool
    department_id: UUID | None
    is_head: bool
    device_id: UUID

async def get_signed_user(request: Request) -> SignedUser:
    """
    Достаёт identity из ECDSA-подписанного payload.
    Проверяет: timestamp freshness, nonce uniqueness, ECDSA signature,
               device активен и не revoked, user активен.
    Возвращает identity из ПОДПИСИ, не из JWT.
    """
    # 1. Извлечь signature/nonce/sig_timestamp/user_id/path из request
    #    (из query для GET, из body для POST/PATCH/DELETE)
    # 2. Проверить timestamp ±5 min
    # 3. Реконструировать payload (тот же формат, что подписывал frontend)
    # 4. INSERT в signature_nonces с UNIQUE → ловим duplicate replay
    # 5. Lookup публичные ключи юзера (user_devices, is_active, NOT revoked)
    # 6. crypto_service.verify_device_signature по каждому ключу
    # 7. Lookup user в БД, is_active
    # 8. Update last_used_at у device
    # 9. Вернуть SignedUser

    # ... детали имплементации ...
```

Замечания по имплементации:
- **Path в payload**: frontend подписывает Engine-путь. Можно либо положить `path` отдельным полем в body и сверять с `request.url.path`, либо канонизировать payload так, что он включает Engine-путь. Решение — см. open question 4.
- **GET vs POST**: для GET signed-параметры приходят в query, body нет. Для POST/PATCH/DELETE — в body. Dependency должен оба варианта понимать.
- **Body в подписи**: cleanBody (без `signature/nonce/sig_timestamp`), JSON.stringify с sorted_keys (Python `json.dumps(sort_keys=True)`).
- **Nonce conflict**: PG `INSERT INTO signature_nonces ... ON CONFLICT DO NOTHING RETURNING nonce`. Если RETURNING пустое → replay → 401.
- **Кэш user-объекта**: можно in-memory TTL 30s, чтобы не делать SELECT users на каждом запросе. Не критично для альфы.

### 4.3. Удалить JWT-логику в Engine

`src/engine/routes/auth.py`:
- **Удалить**: `security`, `create_token`, `verify_token`, `get_current_user`, `JWTBearer`, любые `Depends(get_current_user)`.
- **`POST /auth/login`** (line 131) — оставить как есть в плане проверки пароля и регистрации device key. Перестать возвращать `token`. Возвращать `{user_id, org_id, is_admin, name, username, device_id}`.
- **`POST /auth/register`** — аналогично.
- **`POST /auth/refresh`** — удалить (NestJS делает refresh своими JWT).
- **`POST /auth/verify-signature`** — удалить целиком (внешний оракул больше не нужен).
- **`GET /auth/me`** — оставить, но переключить на `Depends(get_signed_user)`. Возвращает данные юзера, identity из подписи.
- **`GET /auth/devices`** — оставить, переключить на `get_signed_user`.
- **Новое: `DELETE /auth/devices/{device_id}`** — revoke device. Только владелец или admin. Проверка через `get_signed_user`.
- **Новое: `POST /auth/logout`** — синтаксический сахар: revoke текущий device (тот, чьим ключом подписан запрос).

`src/engine/config.py`:
- Удалить `JWT_SECRET`, `JWT_ALGORITHM`, `JWT_EXPIRATION_MINUTES`.
- Добавить `SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS = 300`.
- Добавить `NONCE_TTL_SECONDS = 600` (нонсы хранятся 10 минут — больше окна timestamp).

### 4.4. Переключить ВСЕ роуты на `get_signed_user`

Заменить `Depends(get_current_user)` → `Depends(get_signed_user)` в:
- `src/engine/routes/users.py`
- `src/engine/routes/tasks.py`
- `src/engine/routes/files.py`
- `src/engine/routes/calendar.py`
- `src/engine/routes/notifications.py`
- `src/engine/routes/in_app_notifications.py`
- `src/engine/routes/task_polls.py`
- `src/engine/routes/task_reports.py`
- `src/engine/routes/projects.py`
- `src/engine/routes/departments.py`
- `src/engine/routes/rag.py`
- `src/engine/routes/organizations.py`
- `src/engine/routes/roles.py`

`src/engine/routes/chats.py` — отдельный случай: добавить `Depends(get_signed_user)` ко всем mutation, **удалить** `user_id: UUID = Query(...)` из сигнатур, использовать `signed_user.user_id` внутри. В каждой ручке грепнуть `user_id` (из query) → заменить на `signed_user.user_id`.

`src/engine/routes/health.py` — оставить публичными (нет auth).

### 4.5. Что НЕ требует подписи (`@SkipSignature`-эквивалент)

В Engine FastAPI это просто отсутствие `Depends(get_signed_user)`. Список:
- `/health`, `/health/ready`, `/health/live` — публичные.
- `POST /auth/login`, `POST /auth/register` — bootstrap, подпись здесь невозможна (юзер ещё не зарегистрирован).
- `POST /notifications/telegram/webhook` — приходит снаружи от Telegram, не от webclient. Подписи нет. Защищать через secret-token в URL (TG-механизм).
- `GET /config` (если есть) — публичный maintenance flag.

Legacy `POST /files/upload`, `GET /files/:id/download` — переключить на `get_signed_user`. Это закрывает security-пункт 9.

### 4.6. Тестовый режим

Тесты `pytest` не подписывают запросы. Нужно либо:
- Mock'ать `get_signed_user` через `app.dependency_overrides[get_signed_user] = lambda: SignedUser(...)` в conftest.
- ИЛИ добавить `Config.AUTH_DISABLED = bool(env)` который только в test-env пропускает проверку. Не использовать в проде.

Рекомендую первый вариант (override) — он не оставляет ловушек в проде.

### 4.7. Storage / service-слой

`src/engine/storage/device_storage.py`:
- Добавить методы `revoke(device_id, by_user_id)`, `mark_used(device_id)`, `get_active_keys_by_user(user_id)` (фильтр по `revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())`).

`src/engine/storage/nonce_storage.py` (новый):
- `try_consume(nonce, user_id, ttl_seconds) -> bool` — INSERT с ON CONFLICT, True если новый, False если дубликат.
- `cleanup_expired() -> int` — DELETE WHERE expires_at < NOW(), возвращает count.

`src/engine/services/scheduler_service.py`:
- Добавить периодическую job `cleanup_nonces()` (раз в час).

`src/engine/services/engine_service.py`:
- Регистрация `nonce_storage`.

## 5. Что меняется в NestJS

### 5.1. JwtStrategy — свой секрет

`packages/backend/src/auth/jwt.strategy.ts`:
- `secretOrKey: process.env.NESTJS_JWT_SECRET` (новая env переменная, **не** `ENGINE_JWT_SECRET` или общий `JWT_SECRET`).
- Payload включает `{ user_id, org_id, is_admin, exp }`. Engine токен в `engineToken` больше не нужен — удалить.
- `validate` возвращает `{ id, orgId, isAdmin }` без `engineToken`.

### 5.2. SignatureGuard — упрощается

`packages/backend/src/common/guards/signature.guard.ts`:
- Больше не дёргает Engine `verify-signature` — этого эндпоинта нет.
- Опции:
  - **(a)** Локальная проверка ECDSA силами NestJS (для быстрого отказа). Нужно держать кэш публичных ключей юзеров. NestJS получает их через отдельный signed эндпоинт `GET /auth/devices` или периодический pull.
  - **(b)** Прокидывать signed body как есть в Engine, не проверять. Engine всё равно проверит. Тогда SignatureGuard деградирует до "проверь что поля присутствуют + timestamp window локально". Полная проверка делегирована Engine.
- Рекомендация: **(b)**, минимизирует код и делает Engine единственным источником правды (как ты и сформулировал). Engine на каждом запросе всё равно проверит, дополнительная проверка в NestJS — лишняя сложность с синхронизацией key cache.

После решения: SignatureGuard упрощается до:
```typescript
- проверить что signature/nonce/sig_timestamp/user_id присутствуют в body/query
- проверить timestamp ±300s локально (быстрый отказ — экономим RTT до Engine)
- НЕ дёргает Engine
- НЕ удаляет служебные поля из body (Engine их прочтёт)
- ничего в req.user не пишет (это делает JwtAuthGuard)
```

### 5.3. Новый guard: SignedIdentityGuard

`packages/backend/src/common/guards/signed-identity.guard.ts` (новый):

```typescript
@Injectable()
export class SignedIdentityGuard implements CanActivate {
  canActivate(context: ExecutionContext): boolean {
    const req = context.switchToHttp().getRequest();
    const signedUserId = req.body?.user_id ?? req.query?.user_id;
    const jwtUserId = req.user?.id;

    if (!signedUserId || !jwtUserId) return false;
    if (signedUserId !== jwtUserId) {
      throw new UnauthorizedException(
        'Identity mismatch: JWT user_id != signed user_id'
      );
    }
    return true;
  }
}
```

Применять глобально через `APP_GUARD` после JwtAuthGuard и SignatureGuard. Цель — поймать рассогласование на стороне webclient'а до того, как запрос уйдёт в Engine.

### 5.4. RuGPTEngineAdapter

`packages/backend/src/engine/adapters/rugpt.adapter.ts`:
- Удалить `Authorization: Bearer <engineToken>` (line 210-214).
- Удалить везде `payload.token`, `currentUser.engineToken`.
- Прокидывать **полный signed body** в Engine как есть — `signature/nonce/sig_timestamp/user_id/path` сохранены и Engine их прочтёт.
- Все методы `send_message`, `validate_ai_message`, `reject_ai_message`, etc. — `user_id` в URL/query больше не нужен (Engine достанет его из подписи). Можно убрать.
- `verifySignature` (line 952-957) — удалить, не используется.

### 5.5. Login flow

`packages/backend/src/auth/auth.controller.ts`:
- `POST /auth/login` — прокси к Engine `POST /api/v1/auth/login` (как сейчас). Получает `{user_id, org_id, is_admin, ...}` без token.
- NestJS своими силами выпускает JWT: `this.jwtService.sign({user_id, org_id, is_admin}, { secret: NESTJS_JWT_SECRET, expiresIn: '24h' })`.
- Возвращает frontend `{ token, user, device_id }`.

`packages/backend/src/auth/auth.controller.ts`:
- Удалить `GET /auth/engine-public-key` (если он только для verify-signature был нужен).
- Добавить `POST /auth/logout` — пробрасывает в Engine `POST /api/v1/auth/logout` (signed mutation), возвращает success.

### 5.6. Что НЕ меняется в NestJS

- `WsJwtGuard` для WebSocket — JWT остаётся (переключается на `NESTJS_JWT_SECRET`).
- Frontend обращается к NestJS как сейчас — изменение JWT_SECRET для frontend прозрачно.
- Все controllers — публичный API не меняется, но внутренние использования `req.user.id` / `@Query('userId')` нужно нормализовать (см. 5.7).

### 5.7. Контроллеры — нормализация user_id

`packages/backend/src/chat/chat.controller.ts:76-94`: `senderId: userId` из `@Query('userId')` — заменить на `req.user.id`. Поскольку `SignedIdentityGuard` гарантирует `req.user.id == signed.user_id`, источник теперь однозначен.

Грепнуть весь `packages/backend/src/` по `@Query('userId')`, `@Query('user_id')`, `body.user_id` — заменить на `req.user.id`.

## 6. Что меняется во Frontend

### 6.1. Path в подписываемом payload

`packages/frontend/src/utils/crypto.ts:165-167`:
```typescript
- // Path не включается, т.к. frontend подписывает путь к NestJS, а Engine получает другой путь
- return `${method}:${bodyStr}:${nonce}:${timestamp}`;
+ return `${method}:${path}:${bodyStr}:${nonce}:${timestamp}`;
```

`packages/frontend/src/transport/apiClient.ts:115-150` — `addSignature(method, path, data, userId)`:
- Принимает `path` параметром.
- Передаёт его в `createSignaturePayload`.
- Кладёт `path` отдельным полем в signed body, чтобы Engine мог восстановить (или Engine сверяет с `request.url.path`).

Engine-путь vs NestJS-путь: фронт подписывает **NestJS-путь** (тот, который видит сам, например `/api/tasks`). NestJS прокидывает в Engine со своим путём (`/api/v1/tasks`), но в подписанном body отдельным полем `path = "/api/tasks"`. Engine при проверке использует `path` из body для реконструкции signed payload, а не свой `request.url.path`.

Это требует, чтобы NestJS не подменял body. RuGPTEngineAdapter и так body как есть прокидывает — норм.

Альтернатива (более устойчивая): `path` = логический action key типа `chat.send_message`, фронт ставит, Engine по action key мапит → endpoint allowlist. Это требует поддержки action map'а в обоих концах. Решение — **NestJS-путь в подпись**, действуем по простоте.

### 6.2. Login flow

`packages/frontend/src/app/login/page.tsx`:
- POST `/api/auth/login` → получает `{ token, user, device_id }`.
- Сохраняет `token` в `useAuthStore`, как сейчас.
- Сохраняет `device_id` локально (нужен для logout).

### 6.3. Logout flow

Новая функция `logout()`:
- POST `/api/auth/logout` (signed mutation).
- При успехе: wipe `useAuthStore` (token, user).
- IndexedDB device key — **оставить** (см. 3.4 open question).

### 6.4. user_id в подписи

`apiClient.signedPost(path, body, userId)` — третий аргумент. Источник `userId` — `useAuthStore.user.id`. Это норм: если значение подделано, ECDSA-подпись делается приватным ключом, привязанным к **тому** user_id (через `user_devices` на Engine). Engine проверит подпись по pubkeys реального user_id и отклонит. То есть атакующий-DevTools может подменить только своё клиентское состояние — не identity.

## 7. План миграции пошагово

Каждый шаг — отдельный коммит и желательно отдельный PR. Шаги 2-6 происходят на feature-ветке без выкладки в прод. Шаг 7 — переключение, делается атомарно (одновременный релиз Engine и NestJS).

### Шаг 1. Миграция БД на Engine

`019_signed_auth.sql` — `signature_nonces`, `user_devices.revoked_at/expires_at/last_used_at`. Прогнать на dev и prod (ручной `migrate.sh` на prod).

Безопасно, изменения аддитивные.

### Шаг 2. Engine: `get_signed_user` параллельно с `get_current_user`

Реализовать `dependencies/auth.py`, `storage/nonce_storage.py`. Не подключать никуда. Покрыть юнит-тестами.

### Шаг 3. Engine: новые auth-эндпоинты

- `DELETE /auth/devices/{device_id}` — с `get_signed_user`.
- `POST /auth/logout` — с `get_signed_user`.
- `POST /auth/login` — добавить возврат `device_id`, оставить `token` (пока legacy).

NestJS на этом шаге ещё ничем новым не пользуется.

### Шаг 4. Frontend: path в подпись + device_id

- `crypto.ts:165` → включает path.
- `apiClient.ts:115-150` → принимает path, передаёт в payload.
- Login сохраняет `device_id`.

Это **breaking change** для подписи: NestJS SignatureGuard сейчас реконструирует payload без path. До перевода NestJS на новый формат — frontend и backend несовместимы.

### Шаг 5. NestJS: SignatureGuard под новый формат

- SignatureGuard реконструирует payload **с path**.
- Возможно две версии guard'а с feature-flag (`SIGNATURE_V2`), переключаемых на проде по env.
- На этом шаге всё ещё дёргается Engine `verify-signature`. Логика identity-через-JWT не тронута.

После шага 5 — frontend и NestJS оба умеют новый payload, но Engine ничего не поменял.

### Шаг 6. Engine: переключение роутов на `get_signed_user`

По одному модулю:
1. `routes/health.py` — без изменений (публичный).
2. `routes/users.py` — `Depends(get_current_user)` → `Depends(get_signed_user)`.
3. `routes/tasks.py` — то же.
4. `routes/files.py` — то же + убрать `@SkipSignature`-эквивалент.
5. `routes/calendar.py`, `routes/notifications.py`, `routes/in_app_notifications.py`, `routes/task_polls.py`, `routes/task_reports.py`, `routes/projects.py`, `routes/departments.py`, `routes/rag.py`, `routes/organizations.py`, `routes/roles.py`.
6. `routes/chats.py` — особый случай. Удалить `user_id: UUID = Query(...)` из всех сигнатур, заменить на `signed_user: SignedUser = Depends(get_signed_user)`. Внутри ручки `signed_user.user_id`.

После каждого подшага — e2e-тест: webclient может выполнить операцию, Engine логи показывают использование `get_signed_user`. Если что-то не работает — откатить только этот модуль.

### Шаг 7. Финальное переключение

Когда все Engine-роуты на `get_signed_user`:
- NestJS: SignatureGuard перестаёт дёргать `Engine /auth/verify-signature` (вариант (b) из 5.2). RuGPTEngineAdapter удаляет `Authorization: Bearer engineToken`. JwtStrategy переходит на `NESTJS_JWT_SECRET`. SignedIdentityGuard включается.
- Engine: удаляет `routes/auth.py` функции `create_token`, `verify_token`, `get_current_user`, `POST /auth/refresh`, `POST /auth/verify-signature`. Удаляет `Config.JWT_SECRET`.
- Атомарный релиз обоих сервисов одновременно (deploy.sh обновляет оба).

### Шаг 8. Cleanup

- Frontend: убрать передачу `userId` третьим аргументом везде, где это технически не нужно после нормализации (опционально — это косметика).
- Удалить unused: `auth.controller.ts:18` (`engine-public-key`), `verifySignature` в адаптере, и т.п.
- `tech-debt.md`: вычеркнуть пункты 2 (общий JWT_SECRET), 9 (signed file upload), часть 12 (nonce cache).

## 8. Что НЕ меняется

- Flow ECDSA-ключей на frontend (генерация, IndexedDB).
- Login form / UI.
- WebSocket auth (остаётся JWT, NESTJS_JWT_SECRET).
- Структура `user_devices` — только аддитивные поля.
- Структура чатов / задач / прочей бизнес-логики.
- Telegram webhook auth (как есть).
- Kafka, Scheduler, RAG — не затронуты.
- Async агентные вызовы — `agent_runs.trigger_user_id` сохраняет identity при создании, фоновый consumer не делает повторную проверку подписи (сообщение уже верифицировано в момент HTTP-запроса).

## 9. Открытые вопросы

По каждому пункту — что мы вообще обсуждаем, два варианта, и что я предлагаю выбрать. Финальное слово за тобой.

### 9.1. Что делать с приватным ключом в браузере при logout

Когда юзер жмёт "выйти", приватный ECDSA-ключ лежит в IndexedDB. Можно стирать его вместе с logout, можно оставлять.

Если **оставлять**: на сервере мы помечаем устройство как revoked, дальнейшие запросы падают с 401. При следующем логине Engine видит знакомый pubkey, снимает revoked-флаг, всё работает дальше. Юзер заходит обратно по паролю, без перепейринга устройства.

Если **стирать**: при следующем логине браузер генерит новую пару ключей, в `user_devices` появляется ещё одна строка. Через десяток циклов logout/login там накопится мусор.

Я бы оставлял. Если юзеру нужен честный wipe (например, выходит с чужого компа) — это отдельный жест "забыть это устройство", и он делает обе вещи: revoke на сервере + wipe IndexedDB. Обычный logout этим не должен заниматься.

### 9.2. Заставлять ли устройства протухать сами по себе

Можно ставить `user_devices.expires_at = now() + 90 дней` (или другой срок). Тогда даже если ключ утёк, и мы не заметили — он работает не вечно.

Минус: каждые 90 дней юзер должен логиниться заново на каждом устройстве. На альфе людей мало, проблем без того хватает, а это лишняя точка раздражения.

Предлагаю на альфе не ставить (`expires_at = NULL`). Сам механизм будет уже в коде — добавить проверку срока в `get_signed_user` потом тривиально, одна строка.

### 9.3. Что класть в подпись как path

URL живёт на трёх уровнях:
- что видит фронт: `/api/tasks`
- что видит Engine после nginx-rewrite: `/api/v1/tasks`
- абстрактный action-id: `task.create`

Подпись делает фронт, и проще всего ему подписать тот URL, который он сам зовёт — `/api/tasks`. Это значение фронт кладёт в тело запроса отдельным полем `path`. NestJS прокидывает тело в Engine как есть, Engine при реконструкции payload берёт `path` оттуда.

Альтернатива — подписывать Engine-путь. Тогда фронт должен знать про nginx-rewrite, что плохо: лишняя связь с инфраструктурой.

Action-id (`task.create`) устойчивее к переименованиям роутов, но требует мап-таблицы `action → endpoint` в двух местах. На текущем размере API это overkill.

Рекомендую первое — фронт подписывает свой URL.

### 9.4. Проверять ли подпись в NestJS или сразу пускать в Engine

Сейчас NestJS дёргает Engine `/auth/verify-signature` для каждой подписи. После миграции этого эндпоинта нет. Что делает NestJS:

**Вариант "пропускает дальше"**. Проверяет только что поля `signature`, `nonce`, `sig_timestamp`, `user_id` присутствуют, и что timestamp в окне ±5 минут (это бесплатно, локально). Дальше шлёт запрос в Engine как есть. Битая подпись отвалится с 401 уже от Engine.

**Вариант "сам проверяет ECDSA"**. NestJS держит локальный кеш публичных ключей юзеров, синхронизирует его с Engine, режет битые подписи у себя без round-trip'а. Появляется вторая точка правды для ключей, и её надо инвалидировать при device revoke.

Ты сам сказал — Engine источник правды. Если NestJS делает ту же проверку параллельно, эта формулировка размывается, а выигрыш — один RTT на запросах с битой подписью, которых у нормальных юзеров быть не должно.

Рекомендую "пропускает дальше".

### 9.5. Как обходить подпись в тестах

Pytest сам ECDSA-подписи не делает. Нужно как-то выключать проверку в тестовом процессе. Варианты:

**Флаг в конфиге** (`Config.AUTH_DISABLED = True`). Просто, но опасно: если кто-то случайно прокинет эту переменную на прод (через `.env`, через CI ошибку) — авторизация молча отключается, и заметят это только когда что-то взломают.

**Переопределение FastAPI dependency** (`app.dependency_overrides[get_signed_user] = lambda: SignedUser(...)` в `conftest.py`). Этот хук существует только в тестовом процессе, в проде его физически нет.

Рекомендую второе. Флагов "выключить безопасность" в конфиге не должно быть в принципе.

### 9.6. Где хранить нонсы

Объёмы небольшие — TTL 10 минут, на нагрузке альфы максимум десятки тысяч строк одновременно.

**Redis** — идеален для такого: TTL встроенный, INSERT с автоистечением одной командой, никакого фонового cleanup. Но в Engine Redis не используется (он только на webclient). Поднимать его специально под нонсы — это новый сервис и новая точка отказа.

**PostgreSQL** — уже есть. Таблица `signature_nonces(nonce PRIMARY KEY, expires_at)`, replay ловится UNIQUE-конфликтом на INSERT, просроченные строки чистит фоновый job раз в час. Кода чуть больше, новой инфраструктуры — ноль.

Рекомендую Postgres. Если когда-нибудь нагрузка вырастет настолько, что таблица станет горячей — Redis подложить под это всегда успеем.

### 9.7. Как раскатить без даунтайма

Шаг 7 требует одновременного выката Engine и NestJS: старый формат подписи не работает с новым Engine, новый — не работает со старым NestJS.

**Короткое окно профилактики**. Вешаем на несколько минут баннер "обновление", выкатываем оба сервиса, снимаем баннер. Стандартная практика для альфы.

**Совместимость "и так и так"**. Engine временно понимает оба формата подписи. Сначала выкатываем Engine, убеждаемся что старые webclient'ы продолжают работать, потом катим новый webclient. После — отдельным релизом убираем поддержку старого формата. Без даунтайма, но втрое больше кода и времени.

На альфе — первый вариант. Когда у нас появятся пользователи, для которых пара минут профилактики заметны, перейдём на схему с совместимостью для следующих миграций.

## 10. Файлы — чеклист

### Engine

- [ ] `src/engine/migrations/019_signed_auth.sql` — новая
- [ ] `src/engine/dependencies/auth.py` — новая, `get_signed_user`, `SignedUser`
- [ ] `src/engine/storage/nonce_storage.py` — новая
- [ ] `src/engine/storage/device_storage.py` — добавить методы revoke / mark_used / get_active_keys
- [ ] `src/engine/services/engine_service.py` — регистрация nonce_storage
- [ ] `src/engine/services/scheduler_service.py` — cleanup_nonces job
- [ ] `src/engine/config.py` — удалить JWT_SECRET и связанное, добавить SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS, NONCE_TTL_SECONDS
- [ ] `src/engine/routes/auth.py` — удалить get_current_user / verify_token / create_token / verify-signature / refresh, добавить logout / DELETE devices, login без token
- [ ] `src/engine/routes/users.py` — переключение
- [ ] `src/engine/routes/tasks.py` — переключение
- [ ] `src/engine/routes/files.py` — переключение + убрать SkipSignature
- [ ] `src/engine/routes/calendar.py` — переключение
- [ ] `src/engine/routes/notifications.py` — переключение
- [ ] `src/engine/routes/in_app_notifications.py` — переключение
- [ ] `src/engine/routes/task_polls.py` — переключение
- [ ] `src/engine/routes/task_reports.py` — переключение
- [ ] `src/engine/routes/projects.py` — переключение
- [ ] `src/engine/routes/departments.py` — переключение
- [ ] `src/engine/routes/rag.py` — переключение
- [ ] `src/engine/routes/organizations.py` — переключение
- [ ] `src/engine/routes/roles.py` — переключение
- [ ] `src/engine/routes/chats.py` — снять `user_id: UUID = Query(...)` со всех ручек, добавить get_signed_user
- [ ] Обновить `docs/networking.md`, `docs/api.md` (раздел Auth), `docs/architecture.md` (Zero Trust раздел)
- [ ] Обновить `tech-debt.md` (вычеркнуть закрытые пункты)

### NestJS

- [ ] `packages/backend/src/auth/jwt.strategy.ts` — `NESTJS_JWT_SECRET`, удалить engineToken
- [ ] `packages/backend/src/auth/auth.controller.ts` — login сам выпускает JWT, добавить logout, удалить engine-public-key
- [ ] `packages/backend/src/auth/auth.service.ts` — login flow адаптировать
- [ ] `packages/backend/src/common/guards/signature.guard.ts` — упрощение, payload реконструкция с path, не дёргать verify-signature
- [ ] `packages/backend/src/common/guards/signed-identity.guard.ts` — новый
- [ ] `packages/backend/src/app.module.ts` — регистрация SignedIdentityGuard
- [ ] `packages/backend/src/engine/adapters/rugpt.adapter.ts` — удалить Authorization header, удалить verifySignature, удалить user_id-в-URL прокидывание
- [ ] `packages/backend/src/chat/chat.controller.ts:76-94` — senderId из req.user.id
- [ ] Грэп по `@Query('userId')`, `@Query('user_id')`, `body.user_id` — нормализовать
- [ ] env: добавить `NESTJS_JWT_SECRET`, удалить `ENGINE_JWT_SECRET` (или unused)

### Frontend

- [ ] `packages/frontend/src/utils/crypto.ts:165` — path в payload
- [ ] `packages/frontend/src/transport/apiClient.ts:115-150` — addSignature принимает path
- [ ] `packages/frontend/src/transport/httpClient.ts` — JWT остаётся (просто проверить что не сломали)
- [ ] `packages/frontend/src/app/login/page.tsx` — сохранить device_id
- [ ] Новая функция `logout()` — POST /api/auth/logout

### Tests

- [ ] `tests/conftest.py` — dependency override для get_signed_user в pytest
- [ ] Юнит-тесты `dependencies/auth.py` — replay, expired ts, wrong signature, revoked device
- [ ] e2e-тест полного цикла login → signed mutation → logout

## 11. Риски

- **Race на nonce таблицу**: при высоком RPS возможна горячая точка на INSERT. Митигация — индекс на `expires_at` для быстрого cleanup, при необходимости — переход на Redis.
- **Сломанная подпись после смены формата payload (path)**: пользователи с открытыми сессиями получают 401 после релиза. Митигация — публичный logout-then-login, либо короткий fallback период когда Engine принимает обе версии payload.
- **Тестовая среда без подписи**: легко забыть переопределить dependency, что приведёт к падению всех тестов. Митигация — fixture в conftest.
- **Telegram webhook остаётся без auth**: это known gap, но не часть этой миграции.
- **Async агентный flow**: после публикации в `agent.requests` фоновый consumer работает на основании `agent_runs.trigger_user_id`. Нужно убедиться что `trigger_user_id` пишется из `signed_user.user_id`, не из JWT (его уже нет).

## 12. Критерии готовности

- [ ] Все Engine-роуты используют `Depends(get_signed_user)` (кроме явного списка публичных).
- [ ] `Config.JWT_SECRET` удалён из Engine.
- [ ] Engine `routes/auth.py` не содержит `get_current_user`, `create_token`, `verify_token`, `POST /auth/verify-signature`, `POST /auth/refresh`.
- [ ] NestJS `JwtStrategy` использует `NESTJS_JWT_SECRET`, не общий с Engine.
- [ ] NestJS `SignatureGuard` не дёргает Engine `verify-signature`.
- [ ] NestJS `SignedIdentityGuard` активен глобально.
- [ ] Engine adapter не отправляет `Authorization` header.
- [ ] Frontend подписывает payload с включённым path.
- [ ] e2e тест: подмена `req.body.user_id` отдельно от JWT — 401.
- [ ] e2e тест: replay (тот же nonce второй раз) — 401.
- [ ] e2e тест: подпись старше 5 минут — 401.
- [ ] e2e тест: revoked device — 401.
- [ ] Документация (`docs/networking.md`, `docs/api.md`, `docs/architecture.md`) обновлена.
