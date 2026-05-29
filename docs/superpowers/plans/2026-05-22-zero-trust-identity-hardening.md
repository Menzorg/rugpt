# Zero Trust — Identity Hardening (План B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
> **NO GIT in this repo** (`/root/rugpt` has no `.git`, git forbidden) — skip all commit steps; leave changes in the working tree. All subagents on Opus.

**Goal:** Перестать доверять `user_id`/`org_id` из параметров запроса. Личность брать из device-подписи (Plan A), JWT — только session-gate + сверка `signed==jwt`, `org_id`/роль — из живой user-записи. Target-ресурсы переименовать в `target_user_id`/`target_org_id`.

**Architecture:** `WebSignatureMiddleware` (Plan A) после успешной проверки кладёт `request.state.zt_user_id`. Зависимость `get_current_user` (auth.py): валидирует JWT (gate), сверяет `jwt.user_id == request.state.zt_user_id` (401 при расхождении — закрывает «скелетный ключ»), личность = signed user_id, `org_id`/`is_admin`/`is_head`/`department_id` из `user_storage.get_by_id`. Параметрические роуты домигрируются на `Depends(get_current_user)`; оставшиеся target-параметры переименовываются.

**Tech Stack:** FastAPI, asyncpg, pytest+pytest-asyncio+httpx. Зависит от Plan A (middleware, SignatureService).

**Scope-граница:** Часть B из трёх. Деплой координированно с A+C (формат запроса/route-id меняется ломающе). Переименование target-параметров — контракт, который **Plan C (фронт/NestJS)** обязан зеркалить.

**Identity-модель (locked):** JWT = session-gate (валиден, не протух, user active) + сверка `signed==jwt`; личность — из подписанных запросом данных (signed user_id). См. спек `2026-05-22-zero-trust-engine-enforcement-design.md`.

**Соглашение об именах:**
- ACTOR (кто делает запрос) → НЕ параметр; `current_user = Depends(get_current_user)`.
- TARGET (над кем/чем) → голые `user_id`/`org_id` ⇒ `target_user_id`/`target_org_id` (включая path-шаблоны; фактический URL не меняется, меняется route_id-шаблон → зеркалит Plan C).

---

## File Structure

- Modify: `src/engine/middleware/web_signature.py` — выставить `request.state.zt_user_id`.
- Modify: `src/engine/routes/auth.py` — переписать `get_current_user` (gate+сверка+identity-from-signature+org-from-record).
- Modify (миграция actor→get_current_user + rename target): `routes/chats.py`, `routes/task_polls.py`, `routes/task_reports.py`, `routes/users.py`, `routes/notifications.py`, `routes/in_app_notifications.py`, `routes/files.py`, `routes/calendar.py`, `routes/departments.py`, `routes/organizations.py`, `routes/rag.py`, `routes/tasks.py`.
- Test: `tests/test_get_current_user_identity.py`, `tests/test_zt_state_handshake.py`, + per-route assertions.

**Классификационное правило (применять в каждом роуте):**
- Параметр = **ACTOR**, если он отвечает «кто инициирует запрос» (комментарий `# In real app, get from JWT`, или используется как `viewer`/`actor`/инициатор visibility/ownership). ⇒ удалить параметр, взять из `current_user`.
- Параметр = **TARGET**, если он указывает «над кем/чем действие» (path `/{user_id}`, `/{org_id}`; `assignee_user_id`; `uploaded_by_user_id`; `generated_for_user_id`; `filter_user_id`; «другой» юзер). ⇒ оставить, но голые `user_id`/`org_id` ⇒ `target_user_id`/`target_org_id`. Уже-говорящие имена (`assignee_user_id`, `other_user_id`, `filter_user_id`) НЕ трогать.

---

### Task 1: middleware выставляет `request.state.zt_user_id`

**Files:** Modify `src/engine/middleware/web_signature.py`

- [ ] **Step 1: добавить установку state после успешной проверки**

В `WebSignatureMiddleware.dispatch`, в ветке успешной верификации — сразу ПОСЛЕ `if not ok: return _err(...)` и ПЕРЕД `return await self._forward(...)` — добавить:
```python
        request.state.zt_user_id = str(uid)
```

- [ ] **Step 2: тест handshake**

Создать `tests/test_zt_state_handshake.py`:
```python
import os, time, base64, uuid
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from urllib.parse import quote
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _kp():
    p = ec.generate_private_key(ec.SECP256R1())
    return p, p.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _sign(p, payload): return base64.b64encode(p.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service(); eng = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval("INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'b',$1) RETURNING id", f"b_{uuid.uuid4().hex[:8]}")
        uid = await c.fetchval("INSERT INTO users (id,org_id,username,name,password_hash,email,is_active) VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id", org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _kp()
    await eng.device_storage.register_device(user_id=uid, device_public_key=pem, device_name="t")
    await pool.close()
    return {"uid": str(uid), "org": str(org), "priv": priv, "token": create_token(uid, org)}


@pytest.mark.asyncio(loop_scope="module")
async def test_state_set_after_verify(env):
    # GET /api/v1/web/users проходит подпись → get_current_user внутри роута увидит совпадение → 200
    rid = "/api/v1/users"; n = uuid.uuid4().hex; ts = int(time.time())
    sig = quote(_sign(env["priv"], f"GET:{rid}::{n}:{ts}"), safe="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['uid']}&signature={sig}&nonce={n}&sig_timestamp={ts}&route_id={rid}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200
```
(Этот тест станет осмысленным после Task 2 — `/users` уже использует `get_current_user`; пока проверяем что цепочка middleware→state→роут не падает.)

- [ ] **Step 3: прогон**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_zt_state_handshake.py -v`
Expected: passed (живые PG+Redis).

- [ ] Commit: SKIP (no git).

---

### Task 2: переписать `get_current_user` (gate + сверка + identity-from-signature + org-from-record)

**Files:** Modify `src/engine/routes/auth.py` (функция `get_current_user`, ~строки 93-120); Test `tests/test_get_current_user_identity.py`

- [ ] **Step 1: тесты (TDD)**

Создать `tests/test_get_current_user_identity.py`:
```python
import uuid, types
import pytest, pytest_asyncio, asyncpg, os
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.engine.routes.auth import get_current_user, create_token
from src.engine.services.engine_service import init_engine_service, get_engine_service

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _req(zt_user_id):
    r = types.SimpleNamespace()
    r.state = types.SimpleNamespace()
    if zt_user_id is not None:
        r.state.zt_user_id = zt_user_id
    return r


def _creds(token): return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service(); eng = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as c:
        org = await c.fetchval("INSERT INTO organizations (id,name,slug) VALUES (gen_random_uuid(),'b2',$1) RETURNING id", f"b2_{uuid.uuid4().hex[:8]}")
        uid = await c.fetchval("INSERT INTO users (id,org_id,username,name,password_hash,email,is_active,is_admin) VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true,false) RETURNING id", org, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    await pool.close()
    return {"uid": uid, "org": org, "token": create_token(uid, org)}


@pytest.mark.asyncio(loop_scope="module")
async def test_signed_matches_jwt_ok(env):
    out = await get_current_user(_req(str(env["uid"])), _creds(env["token"]))
    assert out["user_id"] == env["uid"]
    assert out["org_id"] == env["org"]   # из живой user-записи


@pytest.mark.asyncio(loop_scope="module")
async def test_signed_mismatch_jwt_401(env):
    other = uuid.uuid4()
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(str(other)), _creds(env["token"]))
    assert e.value.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_no_zt_state_rejected_401(env):
    # FAIL-CLOSED: нет доказанной подписью личности → 401 (никакого JWT-fallback)
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(None), _creds(env["token"]))
    assert e.value.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_invalid_jwt_401(env):
    with pytest.raises(HTTPException) as e:
        await get_current_user(_req(str(env["uid"])), _creds("garbage.token.xx"))
    assert e.value.status_code == 401
```

- [ ] **Step 2: прогон — упадёт** (сигнатура `get_current_user` ещё без `request`)

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_get_current_user_identity.py -v`
Expected: FAIL (TypeError: missing request / или старое поведение org из JWT).

- [ ] **Step 3: реализация**

В `src/engine/routes/auth.py` заменить функцию `get_current_user` на:
```python
from fastapi import Request

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> dict:
    """JWT = session-gate; личность = device-подписанный user_id (Plan A). FAIL-CLOSED.

    JWT обязан быть валиден и не протух. Запрос ОБЯЗАН пройти WebSignatureMiddleware —
    он выставляет `request.state.zt_user_id`. Нет его → 401 (unsigned). Его user_id
    ОБЯЗАН совпасть с JWT, иначе 401 (склейка чужой подписи с чужим JWT). Никакого
    fallback на голый JWT: JWT-bearer сам по себе личность не доказывает. Личность и
    org_id — из живой user-записи, не из JWT-claim'ов.
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header required")
    payload = verify_token(credentials.credentials)  # gate: подпись JWT + exp
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    jwt_user_id = UUID(payload["user_id"])
    # FAIL-CLOSED: личность ОБЯЗАНА быть доказана device-подписью (Plan A middleware
    # выставляет request.state.zt_user_id). Нет её → запрос не прошёл проверку подписи
    # → 401. НИКАКОГО fallback на «голый» JWT: JWT — bearer-токен (крадётся, виден в
    # транзите скомпрометированному NestJS), сам по себе личность не доказывает.
    # Граница безопасности — этот код, а не конфиг nginx.
    zt_user_id = getattr(request.state, "zt_user_id", None)
    if zt_user_id is None:
        raise HTTPException(status_code=401, detail="Unsigned request")
    if str(jwt_user_id) != str(zt_user_id):
        raise HTTPException(status_code=401, detail="Identity mismatch")
    actor_user_id = UUID(str(zt_user_id))

    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(actor_user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    set_user_id(str(actor_user_id))
    return {
        "user_id": actor_user_id,
        "org_id": user.org_id,
        "is_admin": user.is_admin,
        "department_id": user.department_id,
        "is_head": user.is_head,
    }
```
(Убедиться, что `from fastapi import Request` есть в импортах.)

- [ ] **Step 4: прогон identity-тестов — пройдёт** (4 passed).
Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_get_current_user_identity.py -v`

- [ ] **Step 5: учесть регресс fail-closed на ВСЕЙ суите**

Fail-closed `get_current_user` ломает **любой** существующий тест, который бьёт по роуту с `Depends(get_current_user)` напрямую `/api/v1/*` без подписи (в т.ч. уже-мигрированные роуты: files/tasks/organizations/folders/…). Это ожидаемо и правильно. Стратегия починки (применяется и в задачах 3-8 для затронутых роутов):
- Юнит-тесты логики роута → переопределять зависимость: `app.dependency_overrides[get_current_user] = lambda: {"user_id": ..., "org_id": ..., "is_admin": ...}` (обходит подпись/JWT для проверки именно логики роута).
- E2E/auth-тесты → через `/api/v1/web/*` с подписанным harness (как `tests/test_web_signature_middleware.py`).
Прогнать всю суиту, переписать падающие под одну из двух стратегий, НЕ ослабляя проверок:
Run: `cd /root/rugpt && venv/bin/python -m pytest -q`
Expected: зелёная суита после миграции тестов. Падения строго от смены контракта (нет user_id-параметра / fail-closed) — чинятся, не глушатся.

- [ ] Commit: SKIP (no git).

---

### Миграционные задачи (Task 3+): паттерн

**Канонический before→after** (применять в каждом роуте к ACTOR-параметрам):

Before:
```python
@router.post("/{chat_id}/messages")
async def send_message(chat_id: UUID, request: SendMessageRequest,
                       user_id: UUID,  # In real app, get from JWT
                       org_id: UUID,
                       engine: EngineService = Depends(get_engine)):
    ...
```
After:
```python
from .auth import get_current_user

@router.post("/{chat_id}/messages")
async def send_message(chat_id: UUID, request: SendMessageRequest,
                       current_user: dict = Depends(get_current_user),
                       engine: EngineService = Depends(get_engine)):
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]
    ...
```
Правила:
- ACTOR `user_id`/`org_id` (включая Pydantic request-модели, где они actor) — убрать, взять из `current_user`. Если они были полями request-модели — удалить поля из модели (фронт перестанет их слать, Plan C).
- TARGET голые `user_id`/`org_id` ⇒ `target_user_id`/`target_org_id` (path/query/body). Уже-говорящие (`assignee_user_id`, `other_user_id`, `uploaded_by_user_id`, `generated_for_user_id`, `filter_user_id`) — НЕ трогать.
- Сохранить admin/cross-org логику (она и так на `current_user["is_admin"]`).
- Каждый изменённый роут — тест: actor больше не из параметра (запрос без `user_id` в query/body всё равно работает через JWT+подпись), и target_*-переименование отражено.

---

### Task 3: `routes/chats.py` (самый объёмный — ~12 actor user_id + 3 org_id)

**Files:** Modify `src/engine/routes/chats.py`; Test `tests/test_chats_identity_migration.py`

- [ ] **Step 1:** прочитать файл, для КАЖДОГО хендлера классифицировать по правилу. Заведомо ACTOR (убрать → current_user): `list_my_chats` (132), `get_unread_counts` (145), `create_direct_chat` (161 user_id+org_id), `get_pending_review_messages` (180), `get_reviewed_messages` (194), `get_unvalidated_messages` (210), `add_participant` (234 user_id+org_id; `participant_id` — TARGET, уже говорящее), `list_messages` (301 user_id=viewer→actor), `send_message` (346 user_id+org_id), и аналогично хендлеры на 455/486/547/597 — проверить по телу. TARGET: `chat_id`, `participant_id`, `other_user_id` (в request-модели), `message_id`.
- [ ] **Step 2:** написать тест `tests/test_chats_identity_migration.py` — для пары ключевых роутов (`/my`, `/{chat_id}/messages` POST) через app+middleware+подпись: запрос НЕ содержит `user_id`/`org_id` в query/body, личность приходит из подписи+JWT, ответ не 401/422 из-за отсутствия параметра. (Использовать harness из `tests/test_web_signature_middleware.py`.)
- [ ] **Step 3:** применить паттерн ко всем actor-хендлерам; убрать `user_id`/`org_id` параметры, добавить `current_user=Depends(get_current_user)`, внутри `user_id=current_user["user_id"]` и т.д.
- [ ] **Step 4:** прогон `pytest tests/test_chats_identity_migration.py tests/test_chats_read_routes.py -v` — новые passed; существующие chats-тесты починить под новую сигнатуру (они, вероятно, слали user_id в query — обновить на подпись/JWT-harness или на прямой вызов через current_user). Если существующий тест ломается из-за смены контракта — это ожидаемо, привести его к новому виду, НЕ ослабляя проверки.
- [ ] Commit: SKIP.

---

### Task 4: `routes/task_polls.py` + `routes/task_reports.py`

**Files:** Modify обоих; Test `tests/test_task_polls_reports_identity.py`

- [ ] **Step 1:** прочитать. `task_polls.py`: request-модель с `org_id`(36)+`assignee_user_id`(37) — `org_id` actor? проверить по хендлеру создания опроса (вероятно actor-org). `assignee_user_id` — TARGET (уже говорящее, не трогать). `task_reports.py`: `org_id`(24) actor, `generated_for_user_id`(25) TARGET (не трогать).
- [ ] **Step 2:** тест — оба роута без `org_id` в запросе работают через current_user.
- [ ] **Step 3:** убрать actor `org_id` из request-моделей/сигнатур → `current_user["org_id"]`. Голых target `user_id`/`org_id` тут нет (assignee_/generated_for_ говорящие).
- [ ] **Step 4:** прогон + починка существующих тестов. Commit: SKIP.

---

### Task 5: `routes/users.py`

**Files:** Modify `src/engine/routes/users.py`; Test `tests/test_users_identity.py`

- [ ] **Step 1:** классифицировать. `org_id`(57) в request-модели create — actor? (админ создаёт юзера в СВОЕЙ орге → actor-org из current_user). Path `user_id`(189,256,347,374,418) — это TARGET (над кем действие) ⇒ `target_user_id` (path-шаблон `/{user_id}`→`/{target_user_id}`, route_id меняется → Plan C зеркалит; URL не меняется). Проверить, нет ли отдельного actor user_id рядом.
- [ ] **Step 2:** тест — create-user без `org_id` в теле (берётся из current_user.org_id админа); path target переименован.
- [ ] **Step 3:** применить: actor org_id → current_user; path `{user_id}` → `{target_user_id}` во всех 5 хендлерах + внутри тела функций.
- [ ] **Step 4:** прогон + починка. Commit: SKIP.

---

### Task 6: `routes/notifications.py` + `routes/in_app_notifications.py`

**Files:** Modify обоих; Test `tests/test_notifications_identity.py`

- [ ] **Step 1:** `notifications.py` request-модели (33-34, 46) `user_id`+`org_id` — actor (регистрация канала текущим юзером). `in_app_notifications.py` (26-27) `user_id`+`org_id` — actor. Все actor → current_user, поля убрать из моделей.
- [ ] **Step 2:** тест — оба без user_id/org_id в теле. **Step 3:** применить. **Step 4:** прогон+починка. Commit: SKIP.

---

### Task 7: `routes/files.py` + `routes/calendar.py` + `routes/departments.py`

**Files:** Modify трёх; Test `tests/test_files_calendar_dept_identity.py`

- [ ] **Step 1:** `files.py` request-модель (31-33): `user_id`+`org_id` actor → current_user; `uploaded_by_user_id` — TARGET говорящее (не трогать). NB: files.py уже частично на current_user (видно is_admin checks) — привести к единообразию. `calendar.py` `org_id`(46) actor → current_user. `departments.py` `user_id`(95) — TARGET (кого назначить главой) ⇒ `target_user_id`.
- [ ] **Step 2:** тесты на три роута. **Step 3:** применить (actor→current_user; departments target rename). **Step 4:** прогон+починка. Commit: SKIP.

---

### Task 8: `routes/organizations.py` + `routes/rag.py` + `routes/tasks.py` (доурегулировать)

**Files:** Modify трёх; Test `tests/test_orgs_rag_tasks_identity.py`

- [ ] **Step 1:** `organizations.py` path `org_id`(91,115,161,187) — TARGET (какую оргу) ⇒ `target_org_id` (path-шаблон меняется, URL нет). Actor — уже из current_user (есть is_admin checks). `rag.py` `filter_user_id`(102) — TARGET говорящее, НЕ трогать (это alias=user_id фильтр; оставить). `tasks.py` `user_id`(710) — классифицировать (actor или target); большинство tasks уже на current_user/`_load_user`.
- [ ] **Step 2:** тесты. **Step 3:** применить (organizations path → target_org_id). **Step 4:** прогон+починка. Commit: SKIP.

---

## Self-Review (выполнено при написании)

- Покрытие спека: identity из signed-данных (Task 2), JWT-gate+сверка (Task 1+2), org из живой записи (Task 2), миграция actor-параметров (Task 3-8), target_* rename (Task 5,7,8 где есть голые target). Cross-org/admin сохраняется (на current_user["is_admin"]).
- Открытые места для исполнителя: точная actor/target классификация делается ЧТЕНИЕМ каждого файла по правилу выше — это не плейсхолдер, а явная процедура с перечнем сайтов и говорящих исключений.
- Зависимость: Task 2 требует Task 1 (request.state.zt_user_id). Все миграции требуют Task 2.

## Связь с Планом C
Переименование actor→(нет параметра) и target→`target_*`, удаление actor-полей из request-моделей и смена path-шаблонов — это контракт. Plan C: фронт перестаёт слать `user_id`/`org_id` как actor, шлёт `target_*` где нужно, route-id-реестр зеркалит новые шаблоны.
