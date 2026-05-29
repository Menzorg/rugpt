# Zero Trust — Client Cutover (План C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (или executing-plans). Checkbox steps.
> **NO GIT** (`/root/webclient_rugpt` — git forbidden by user): skip commits, leave changes in working tree. Subagents on Opus.
> **Repo:** этот план трогает `/root/webclient_rugpt` (frontend Next.js + backend NestJS + packages/common), НЕ `/root/rugpt`.

**Goal:** Перевести вебклиент на новый Zero Trust контракт движка (Планы A+B): фронт подписывает `route_id` (engine-шаблон из общего реестра), NestJS становится прозрачным реле сырого подписанного тела на `/api/v1/web/*`, удаляется side-call verify-signature, зеркалятся target_*-переименования Plan B.

**Architecture:** payload = `METHOD:route_id:clean_body:nonce:timestamp`. `route_id` = engine route-шаблон (напр. `/api/v1/chats/{chat_id}/messages`) из реестра в `packages/common`. Фронт подписывает device-ключом (ECDSA P-256, IndexedDB) и шлёт на NestJS. NestJS **форвардит сырое тело + signature/nonce/sig_timestamp/user_id/route_id нетронутыми** на движок `/api/v1/web/<engine-path>` (не реконструирует через DTO→command). Движок (Plan A middleware) проверяет подпись, (Plan B) `get_current_user` берёт личность из подписи. Транспорт case-не-ломает (нет глобального case-интерцептора; только LoggingInterceptor).

**Tech Stack:** TS, Next.js, NestJS, axios, Web Crypto API; frontend tests = **vitest**, backend tests = **jest**.

**Scope-граница:** Часть C из трёх. Деплой координированно с A+B (одновременно). Прод-nginx должен форвардить `/api/v1/web/*` нетронутым (Plan A nginx-task) — проверяет оператор.

## Контракт канонизации (должен совпасть с движком, Python)
- Python (движок): `json.dumps(clean_body, separators=(",",":"), sort_keys=True, ensure_ascii=False)`.
- TS (фронт): `JSON.stringify(sortKeysRecursively(clean_body))` (компактно, рекурсивная сортировка, unicode). `sortKeysRecursively` уже есть в `apiClient.ts` — вынести в общий util.
- `clean_body` = тело без `{signature, nonce, sig_timestamp, route_id}` (но С `user_id` — он нужен движку для поиска device-ключей; движок-роут его игнорит, личность из get_current_user). GET → `""`.
- **`org_id` фронт больше НЕ шлёт** (движок выводит из user-записи, Plan B). `target_*` шлёт где Plan B переименовал.

## Реальность вебклиента (разведано)
- Фронт подписывает **NestJS-пути** (`api.signedGet('/api/tasks')`), не engine-шаблоны → нужен реестр.
- Адаптер шлёт на `/api/v1/*` (без `/web`) → переключить на `/api/v1/web/*`.
- `SignatureGuard` (`packages/backend/src/common/guards/signature.guard.ts`) делает side-call `verifySignature` → удалённый `/auth/verify-signature` → удалить весь verify-flow.
- Контроллеры реконструируют payload (DTO→`execute(command)`) → для signed-роутов нужен generic-реле сырого тела.
- `createSignaturePayload` (`packages/frontend/src/utils/crypto.ts`): `method:body:nonce:timestamp` → добавить route_id.

---

## File Structure
- Create: `packages/common/src/zt-routes.ts` — реестр route-id (action → engine route-шаблон) + хелпер подстановки path-параметров.
- Create: `packages/common/src/zt-canonical.ts` — `sortKeysRecursively` + `canonicalBody()` (общий util, единый с движком).
- Modify: `packages/frontend/src/utils/crypto.ts` — `createSignaturePayload(method, route_id, body, nonce, ts)`.
- Modify: `packages/frontend/src/transport/apiClient.ts` — signed-методы кладут route_id (из реестра), не шлют org_id, юзают общий canonical util; multipart-подпись.
- Modify: `packages/backend/src/engine/adapters/rugpt.adapter.ts` — `/api/v1/web/*`; generic `relaySigned(method, enginePath, rawBody, headers)`; убрать `verify_signature` command.
- Modify: `packages/backend/src/common/guards/signature.guard.ts` — удалить verify-call (движок проверяет); гард либо снять, либо свести к проверке наличия полей (без вызова движка).
- Modify: signed-контроллеры/сервисы — для signed-роутов форвардить сырое тело через relaySigned, не реконструировать.
- Test: `packages/common` vitest/jest — parity-тест канонизации (TS-вывод == зафиксированный Python-эталон).

---

### Task 1: общий canonical util + parity-фикстура
- Create `packages/common/src/zt-canonical.ts`: `sortKeysRecursively(obj)` (рекурсивно сортит ключи объектов, массивы — поэлементно) + `canonicalBody(obj)` = `JSON.stringify(sortKeysRecursively(stripSignatureFields(obj)))` где stripSignatureFields убирает `{signature,nonce,sig_timestamp,route_id}`.
- Export из `packages/common/src/index.ts`.
- Test (parity): зафиксировать эталонные строки, идентичные выводу движкового `build_payload`/`json.dumps(...,separators=(",",":"),sort_keys=True,ensure_ascii=False)`. Кейсы: вложенные объекты, unicode (`привет`), пустой GET-body. Сверить байт-в-байт с зафиксированными Python-эталонами (взять из `/root/rugpt/tests/test_signature_payload.py` ожидаемых строк).
- TDD: тест → util → green.

### Task 2: реестр route-id в common
- Create `packages/common/src/zt-routes.ts`: объект `ZT_ROUTES` маппинг логического action → engine route-шаблон (БЕЗ `/web`), напр. `CHAT_SEND_MESSAGE: "/api/v1/chats/{chat_id}/messages"`, `TASKS_LIST: "/api/v1/tasks"`, и т.д. Шаблоны ОБЯЗАНЫ совпадать с фактическими роутами движка (включая Plan B target_*-переименования: `/api/v1/users/{target_user_id}`, `/api/v1/organizations/{target_org_id}`, `/api/v1/departments/{department_id}/head/{target_user_id}`).
- Хелпер `fillRoute(template, params)` подставляет `{x}` → значения (для route_id с конкретными id — но route_id ПОДПИСЫВАЕТСЯ как ШАБЛОН, не с подставленными id; движок матчит шаблон. Уточнить: route_id в подписи = шаблон `/api/v1/chats/{chat_id}/messages` дословно; конкретный chat_id идёт в URL/path отдельно). → подписываем ШАБЛОН.
- Полный список собрать из адаптерного command-switch (`rugpt.adapter.ts`) — каждый signed action.
- Export. Тест: реестр покрывает все signed-actions; шаблоны валидны.
- **Sync-guard:** добавить заметку/скрипт-идею, что реестр должен соответствовать роутам движка (ручная сверка при изменении роутов; в будущем — кодген).

### Task 3: frontend createSignaturePayload + route_id
- `packages/frontend/src/utils/crypto.ts`: `createSignaturePayload(method, routeId, body, nonce, timestamp)` → `${METHOD}:${routeId}:${body}:${nonce}:${timestamp}`.
- TDD (vitest): payload-формат с route_id; GET пустой body.

### Task 4: apiClient signed-методы (route_id + drop org_id + canonical util)
- `addSignature`/signed* принимают `routeId` (из реестра, по action) ; `bodyForSignature` = `{...data, user_id}` (БЕЗ org_id) ; canonical через общий `canonicalBody`; payload через новый `createSignaturePayload(method, routeId, canonicalBody, nonce, ts)`.
- На wire: POST body содержит `{...data, user_id, signature, nonce, sig_timestamp, route_id}` ; GET — те же в query (signature url-encoded, как сейчас).
- Call-sites (`hooks/*`) — передавать route_id (по action из реестра); убрать передачу org_id; использовать target_* поля где Plan B переименовал.
- TDD (vitest): signed POST/GET формируют корректный payload+поля; org_id отсутствует.

### Task 5: ПОЛНАЯ multipart-подпись (files/org-context/invoice upload; download — GET, обычная подпись)
**Решение:** делаем сразу, с парной правкой движкового middleware. files-роуты остаются ПОД подписью (НЕ в no-signature). Это кросс-репо задача: движок (`/root/rugpt`) + фронт (`/root/webclient_rugpt`).

**Схема подписи multipart (единая фронт↔движок):**
- body-репрезентация multipart = canonical JSON от `{<не-файловые form-поля БЕЗ signature-полей>, user_id, file_sha256: <hex sha256 содержимого файла>}` (через тот же `canonicalBody`/`json.dumps(...,sort_keys=True,separators=(",",":"),ensure_ascii=False)`).
- payload = `METHOD:route_id:<этот canonical>:nonce:timestamp`. Так привязаны и личность, и не-файловые поля (`target_user_id`, `is_public`, …), и содержимое файла.
- signature/nonce/sig_timestamp/user_id/route_id передаются как **form-поля** рядом с multipart (не как файл).

**Движковая правка (`/root/rugpt/src/engine/middleware/web_signature.py` + сервис):**
- В `dispatch`: если `Content-Type: multipart/form-data` И роут требует подпись — НЕ парсить JSON. Прочитать multipart (starlette `await request.form()`), достать файл(ы) и не-файловые поля + signature-поля.
- Вычислить `file_sha256 = sha256(file_bytes).hexdigest()`; собрать `signed_dict = {<не-файловые поля минус signature-поля>, "file_sha256": file_sha256}` (user_id уже среди полей).
- `payload = build_payload_multipart(method, route_id, signed_dict, nonce, ts)` — переиспользовать canonical (`signature_payload.build_payload` с этим dict как body).
- Дальше как обычно: route-id match, `signature_service.verify_request_signature`, nonce, форвард (важно: тело файла НЕ должно быть «съедено» — `request.form()` кеширует; при форварде downstream-роут должен получить multipart целым. Проверить, что `request._body`/stream переотдаётся; если starlette не позволяет переиграть multipart-stream после `.form()`, использовать чтение `await request.body()` сырых байт + вычислить sha256 из распарсенного файла, и переотдать сырые байты через `request._body`, как в JSON-ветке).
- Тест движка (`/root/rugpt/tests/`): подписанный multipart upload проходит; подмена файла (другой sha) → 401; чужой route_id → 401.

**Фронт (`/root/webclient_rugpt`):**
- В `apiClient` добавить `signedUpload(routeId, file, fields, userId)`: `file_sha256 = sha256(await file.arrayBuffer())` (Web Crypto `crypto.subtle.digest('SHA-256', ...)` → hex); `signedDict = canonicalBody({...fields, user_id, file_sha256})`; `payload = createSignaturePayload(method, routeId, signedDict, nonce, ts)`; signature; собрать `FormData` с файлом + всеми form-полями + signature-полями; POST на NestJS.
- download (GET) — обычная signedGet (без файла), route_id из реестра.
- Call-sites: `useFiles`/org-context/invoice upload → `signedUpload`.
- Тест (vitest): payload-сборка для multipart детерминирована; sha256 совпадает с зафиксированным эталоном (сверить с движковым `hashlib.sha256` на тех же байтах).

**NestJS:** generic-реле (Task 6) для multipart — форвардить multipart-тело + signature form-поля на `/api/v1/web/<path>` нетронутым (FileInterceptor на signed-роутах НЕ должен переформатировать; реле передаёт поток как есть).

### Task 6: NestJS generic signed-relay
- `rugpt.adapter.ts`: метод `relaySigned(method, enginePath, rawBody, headers)` — шлёт `rawBody` (включая signature-поля + user_id + route_id) на `${engineUrl}/api/v1/web${enginePath}` нетронутым; correlation_id в query.
- Signed-контроллеры/сервисы: вместо DTO→`execute(command, {реконструкция})` для signed-роутов вызывать `relaySigned` с СЫРЫМ телом запроса. (Спроектировать: либо общий signed-passthrough контроллер/перехват, либо точечно в каждом signed-роуте.)
- `engineUrl` остаётся base; путь получает префикс `/api/v1/web`.
- Тест (jest): relaySigned форвардит тело байт-в-байт, не мутирует; путь = `/api/v1/web/...`.

### Task 7: убрать verify-signature side-call + @SkipSignature
- `signature.guard.ts`: удалить вызов `engineAdapter.verifySignature` и retry/503-логику (движок проверяет в middleware). Гард: либо удалить целиком (enforcement на движке), либо оставить тонкую проверку наличия signature-полей (быстрый 401 без вызова движка). Решить на ревью.
- Удалить `verify_signature` command из адаптера.
- `@SkipSignature`-декораторы: публичный набор теперь = движковый `WEB_NO_SIGNATURE_ROUTES`; привести в соответствие (или удалить вместе с гардом).
- Тест: signed-запрос проходит без NestJS-side-call; публичные роуты не требуют подписи.

### Task 8: прод-nginx + интеграционная сверка (verify)
- Прод-шаг (оператор): nginx форвардит `/api/v1/web/*` нетронутым (Plan A). Команда сверки:
  `sudo nginx -T 2>/dev/null | grep -n "api/v1/web"`
- Координированный деплой A+B+C. Юзеры перелогиниваются (device-ключ регистрируется).
- E2E (по возможности локально): фронт-подпись → NestJS-реле → движок принимает (200), битая подпись → 401.

---

## Решённые вопросы
1. **Multipart-подпись** (Task 5): **делаем сразу** с парной движковой правкой (file_sha256 в canonical). files-роуты остаются под подписью. (Решено: «не откладывать».)
2. **NestJS relay-механизм** (Task 6): generic signed-passthrough vs точечно — решает имплементер при чтении контроллеров (рек.: общий passthrough-слой если роуты однородны).
3. **SignatureGuard** (Task 7): рек. — тонкая проверка-наличия signature-полей (быстрый 401 без вызова движка), verify-call в движок удалить.
4. **Реестр route-id sync** (Task 2): рек. — ручная сверка сейчас + заметка про будущий кодген из `app.routes`. Расхождение реестра с движком → 401 Route mismatch (ловится тестом парности + e2e).

## Self-Review
- Покрытие: canonical-parity (T1), реестр route-id (T2), фронт payload+route_id (T3), apiClient drop-org_id+target_* (T4), multipart (T5, под вопросом), NestJS реле (T6), убрать verify side-call (T7), nginx+деплой (T8).
- Зависит от A+B (движок готов). Деплой только координированно.
- НЕ плейсхолдеры: открытые вопросы — явные решения для ревью, влияющие на scope, а не недосказанность.
