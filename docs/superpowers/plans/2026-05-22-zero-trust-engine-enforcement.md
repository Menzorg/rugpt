# Zero Trust — Engine Enforcement (План A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести проверку Zero Trust-подписей с NestJS в сам движок rugpt — как middleware, который проверяет подпись против device-ключей из БД, nonce-replay через Redis, timestamp-окно и привязку действия (route-id) ещё до бизнес-роута.

**Architecture:** Starlette `BaseHTTPMiddleware` перехватывает `/api/v1/web/*`, проверяет allowlist → no-signature-список → timestamp → ECDSA-подпись (перебор device-ключей user_id) → route-id против роут-тейбла FastAPI → nonce в Redis, затем переписывает `/web`→`/api/v1` и форвардит. Источник истины для route-id — сам роут-тейбл движка (отдельного реестра нет). Nonce fail-closed (нет Redis → 503).

**Tech Stack:** Python, FastAPI/Starlette, asyncpg, `cryptography` (ECDSA P-256, уже есть), `redis.asyncio` (добавляем), pytest + pytest-asyncio + httpx ASGITransport.

**Scope-граница:** Это План A из трёх. **Не разворачивать в одиночку** — формат payload меняется ломающе; деплой только вместе с Планами B (hardening `org_id`/`user_id`) и C (клиентский cutover). Тесты гоняются изолированно через синтетические подписанные запросы.

**Предусловия для тестов:** локальный PostgreSQL (`DATABASE_URL`, дефолт `postgresql://postgres@localhost/rugpt`) и локальный Redis (`REDIS_URL`, дефолт `redis://localhost:6379/0`) — как и остальные интеграционные тесты движка, инфра реальная.

**Канонический формат payload (контракт со стороной фронта, План C):**
`payload = "{METHOD}:{route_id}:{clean_body}:{nonce}:{timestamp}"`
- `route_id` — шаблон роута движка без `/web`, напр. `/api/v1/chats/{chat_id}/messages`.
- `clean_body` — тело без полей `{signature, nonce, sig_timestamp, route_id}`, сериализованное `json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)`. Для GET — пустая строка.

---

## File Structure

- Create: `src/engine/security/__init__.py` — пакет.
- Create: `src/engine/security/signature_payload.py` — сборка канонического payload + список служебных полей.
- Create: `src/engine/security/route_match.py` — резолв шаблона роута движка по пути.
- Create: `src/engine/services/nonce_store.py` — Redis-хранилище nonce (`NonceStore`), fail-closed.
- Create: `src/engine/middleware/__init__.py` — пакет.
- Create: `src/engine/middleware/web_signature.py` — `WebSignatureMiddleware`.
- Modify: `src/engine/config.py` — константы allowlist/no-signature/TTL/tolerance.
- Modify: `src/engine/services/crypto_service.py` — добавить `check_timestamp`.
- Modify: `src/engine/services/engine_service.py:446-571,597-` — поднять/закрыть `NonceStore`.
- Modify: `src/engine/app.py:74-87,134-` — подключить middleware.
- Modify: `src/engine/routes/auth.py:253-283` — удалить `POST /auth/verify-signature`.
- Modify: `requirements.txt` — добавить `redis`.
- Test: `tests/test_signature_payload.py`, `tests/test_route_match.py`, `tests/test_nonce_store.py`, `tests/test_crypto_timestamp.py`, `tests/test_web_signature_middleware.py`.
- Deploy: nginx-конфиг на rugpt-container (ручной шаг на проде).

---

### Task 1: Зависимость redis + конфиг-константы

**Files:**
- Modify: `requirements.txt`
- Modify: `src/engine/config.py:42-46` (рядом с Redis settings)

- [ ] **Step 1: Добавить redis в requirements**

В `requirements.txt` добавить строку:

```
redis>=5.0
```

- [ ] **Step 2: Установить**

Run: `/root/rugpt/venv/bin/pip install "redis>=5.0"`
Expected: `Successfully installed redis-...`

- [ ] **Step 3: Добавить константы Zero Trust в Config**

В `src/engine/config.py` сразу после блока Redis settings (после строки с `REDIS_URL`, ~строка 46) вставить:

```python
    # ---- Zero Trust: web signature enforcement ----
    # Окно валидности подписи и TTL nonce (защита от replay). Оба = 5 минут.
    SIG_TIMESTAMP_TOLERANCE_SECONDS = int(os.getenv("SIG_TIMESTAMP_TOLERANCE_SECONDS", "300"))
    NONCE_TTL_SECONDS = int(os.getenv("NONCE_TTL_SECONDS", "300"))

    # Префикс web-роутов, на которые навешивается проверка подписи.
    WEB_PREFIX = "/api/v1/web"

    # Роуты (суффикс после /api/v1/web), доступные через web. Остальное → 404.
    WEB_ALLOWED_ROUTES = [
        "/auth", "/users", "/roles", "/chats", "/organizations",
        "/calendar", "/notifications", "/in-app-notifications",
        "/tasks", "/task-polls", "/task-reports", "/projects",
        "/files", "/folders", "/rag", "/departments", "/support",
        "/corrections", "/actions", "/invoices", "/config", "/health",
    ]

    # Роуты без проверки подписи (pre-auth / server-to-server).
    WEB_NO_SIGNATURE_ROUTES = [
        "/auth/login",
        "/auth/engine-public-key",
        "/config",
        "/health",
        "/notifications/telegram/webhook",
    ]
```

- [ ] **Step 4: Проверить импорт конфига**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.config import Config; print(Config.NONCE_TTL_SECONDS, Config.WEB_PREFIX, len(Config.WEB_ALLOWED_ROUTES))"`
Expected: `300 /api/v1/web 22`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt src/engine/config.py
git commit -m "feat(zt): add redis dep and web-signature config constants"
```

---

### Task 2: `crypto_service.check_timestamp`

**Files:**
- Modify: `src/engine/services/crypto_service.py` (добавить функцию)
- Test: `tests/test_crypto_timestamp.py`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_crypto_timestamp.py`:

```python
import time
from src.engine.services.crypto_service import check_timestamp


def test_fresh_timestamp_ok():
    assert check_timestamp(int(time.time()), tolerance=300) is True


def test_old_timestamp_rejected():
    assert check_timestamp(int(time.time()) - 301, tolerance=300) is False


def test_future_timestamp_rejected():
    assert check_timestamp(int(time.time()) + 301, tolerance=300) is False


def test_boundary_inclusive():
    assert check_timestamp(int(time.time()) - 300, tolerance=300) is True
```

- [ ] **Step 2: Запустить — упадёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_crypto_timestamp.py -v`
Expected: FAIL — `ImportError: cannot import name 'check_timestamp'`

- [ ] **Step 3: Реализовать**

В `src/engine/services/crypto_service.py` добавить в конец файла:

```python
import time as _time


def check_timestamp(timestamp: int, tolerance: int) -> bool:
    """True если |now - timestamp| <= tolerance (секунды)."""
    try:
        return abs(int(_time.time()) - int(timestamp)) <= tolerance
    except (ValueError, TypeError):
        return False
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_crypto_timestamp.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/engine/services/crypto_service.py tests/test_crypto_timestamp.py
git commit -m "feat(zt): add check_timestamp to crypto_service"
```

---

### Task 3: Сборка канонического payload

**Files:**
- Create: `src/engine/security/__init__.py`
- Create: `src/engine/security/signature_payload.py`
- Test: `tests/test_signature_payload.py`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_signature_payload.py`:

```python
from src.engine.security.signature_payload import build_payload, SIGNATURE_FIELDS


def test_get_payload_has_empty_body():
    p = build_payload("GET", "/api/v1/users", body=None, nonce="abc", timestamp=111)
    assert p == "GET:/api/v1/users:" + ":abc:111"  # clean_body == ""


def test_post_payload_sorts_keys_recursively_and_compact():
    body = {"b": 1, "a": {"z": 2, "y": 3}, "signature": "X", "nonce": "n", "route_id": "/x"}
    p = build_payload("POST", "/api/v1/chats", body=body, nonce="n", timestamp=222)
    # signature/nonce/route_id вырезаны; ключи рекурсивно отсортированы; компактно
    assert p == 'POST:/api/v1/chats:{"a":{"y":3,"z":2},"b":1}:n:222'


def test_signature_fields_constant():
    assert SIGNATURE_FIELDS == {"signature", "nonce", "sig_timestamp", "route_id"}


def test_unicode_preserved():
    p = build_payload("POST", "/api/v1/chats", body={"t": "привет"}, nonce="n", timestamp=1)
    assert "привет" in p
```

- [ ] **Step 2: Запустить — упадёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_signature_payload.py -v`
Expected: FAIL — `ModuleNotFoundError: src.engine.security`

- [ ] **Step 3: Реализовать**

Создать `src/engine/security/__init__.py` (пустой).

Создать `src/engine/security/signature_payload.py`:

```python
"""Каноническая сборка payload для проверки Zero Trust-подписи.

Контракт с фронтом (packages/frontend): payload == фронтовый payload байт-в-байт.
clean_body == json.dumps(separators=(",",":"), sort_keys=True, ensure_ascii=False).
"""
import json
from typing import Optional, Dict, Any

# Служебные поля подписи — НЕ входят в clean_body.
SIGNATURE_FIELDS = {"signature", "nonce", "sig_timestamp", "route_id"}


def _clean_body_str(body: Optional[Dict[str, Any]]) -> str:
    if not body:
        return ""
    clean = {k: v for k, v in body.items() if k not in SIGNATURE_FIELDS}
    return json.dumps(clean, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def build_payload(
    method: str,
    route_id: str,
    body: Optional[Dict[str, Any]],
    nonce: str,
    timestamp: int,
) -> str:
    """payload = METHOD:route_id:clean_body:nonce:timestamp. Для GET body=None → пустой."""
    clean = "" if method.upper() == "GET" else _clean_body_str(body)
    return f"{method.upper()}:{route_id}:{clean}:{nonce}:{timestamp}"
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_signature_payload.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/engine/security/__init__.py src/engine/security/signature_payload.py tests/test_signature_payload.py
git commit -m "feat(zt): canonical signature payload builder"
```

---

### Task 4: Резолв шаблона роута движка (route-id источник истины)

**Files:**
- Create: `src/engine/security/route_match.py`
- Test: `tests/test_route_match.py`

`route_id` валидируется так: после rewrite `/web`→`/api/v1` middleware матчит путь против `app.routes` и берёт шаблон совпавшего роута (напр. `/api/v1/chats/{chat_id}/messages`). Подписанный `route_id` должен совпасть с этим шаблоном.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_route_match.py`:

```python
from starlette.routing import Route
from src.engine.security.route_match import resolve_route_template


def _routes():
    async def h():  # pragma: no cover
        return None
    return [
        Route("/api/v1/users", endpoint=h, methods=["GET"]),
        Route("/api/v1/chats/{chat_id}/messages", endpoint=h, methods=["POST"]),
    ]


def test_matches_static():
    assert resolve_route_template(_routes(), "GET", "/api/v1/users") == "/api/v1/users"


def test_matches_param_path():
    tmpl = resolve_route_template(_routes(), "POST", "/api/v1/chats/abc-123/messages")
    assert tmpl == "/api/v1/chats/{chat_id}/messages"


def test_no_match_returns_none():
    assert resolve_route_template(_routes(), "GET", "/api/v1/nope") is None


def test_method_mismatch_returns_none():
    assert resolve_route_template(_routes(), "DELETE", "/api/v1/users") is None
```

- [ ] **Step 2: Запустить — упадёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_route_match.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать**

Создать `src/engine/security/route_match.py`:

```python
"""Резолв шаблона роута движка через стандартный матчинг Starlette.

Источник истины для route-id — сам роут-тейбл приложения, отдельного
реестра нет. Фронт зеркалит те же шаблоны (см. План C, тест парности).
"""
from typing import Iterable, Optional
from starlette.routing import Route, Match


def resolve_route_template(routes: Iterable, method: str, path: str) -> Optional[str]:
    """Вернуть .path шаблона роута, совпавшего по path+method, иначе None."""
    method = method.upper()
    for route in routes:
        if not isinstance(route, Route):
            continue
        scope = {"type": "http", "method": method, "path": path}
        match, _ = route.matches(scope)
        if match == Match.FULL:
            if route.methods and method not in route.methods:
                continue
            return route.path
    return None
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_route_match.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/engine/security/route_match.py tests/test_route_match.py
git commit -m "feat(zt): resolve engine route template for route-id binding"
```

---

### Task 5: `NonceStore` (Redis, fail-closed)

**Files:**
- Create: `src/engine/services/nonce_store.py`
- Test: `tests/test_nonce_store.py`

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_nonce_store.py`:

```python
import os
import uuid
import pytest
import pytest_asyncio
from src.engine.services.nonce_store import NonceStore, NonceStoreUnavailable

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@pytest_asyncio.fixture
async def store():
    s = NonceStore(REDIS_URL, ttl_seconds=2)
    await s.init()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_first_use_ok_replay_rejected(store):
    user_id = str(uuid.uuid4())
    nonce = uuid.uuid4().hex
    assert await store.check_and_store(user_id, nonce) is True   # первый раз
    assert await store.check_and_store(user_id, nonce) is False  # replay


@pytest.mark.asyncio
async def test_unavailable_raises():
    s = NonceStore("redis://localhost:1/0", ttl_seconds=2)  # битый порт
    await s.init()
    with pytest.raises(NonceStoreUnavailable):
        await s.check_and_store("u", "n")
    await s.close()
```

- [ ] **Step 2: Запустить — упадёт**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_nonce_store.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать**

Создать `src/engine/services/nonce_store.py`:

```python
"""Redis-хранилище nonce для защиты от replay. Fail-closed: нет Redis → исключение."""
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.engine.unified_logger import get_logger

logger = get_logger("services")


class NonceStoreUnavailable(Exception):
    """Redis недоступен — запрос нельзя безопасно пропустить (fail-closed)."""


class NonceStore:
    def __init__(self, redis_url: str, ttl_seconds: int):
        self._url = redis_url
        self._ttl = ttl_seconds
        self._redis: Redis | None = None

    async def init(self) -> None:
        self._redis = Redis.from_url(self._url, socket_connect_timeout=2)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def check_and_store(self, user_id: str, nonce: str) -> bool:
        """True если nonce новый. False если уже использован. Raise при недоступности."""
        if self._redis is None:
            raise NonceStoreUnavailable("NonceStore not initialized")
        key = f"nonce:{user_id}:{nonce}"
        try:
            result = await self._redis.set(key, "1", nx=True, ex=self._ttl)
        except (RedisError, OSError) as e:
            logger.error("nonce store unavailable", operation="check_nonce", error=str(e))
            raise NonceStoreUnavailable(str(e)) from e
        return result is not None
```

- [ ] **Step 4: Запустить — пройдёт** (нужен локальный Redis)

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_nonce_store.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/engine/services/nonce_store.py tests/test_nonce_store.py
git commit -m "feat(zt): redis-backed NonceStore, fail-closed"
```

---

### Task 6: Поднять `NonceStore` в `EngineService`

**Files:**
- Modify: `src/engine/services/engine_service.py` (конструктор ~94, `initialize` ~446-484, `close` ~540-571)

- [ ] **Step 1: Создать инстанс в конструкторе**

В `src/engine/services/engine_service.py` после строки `self.invoice_storage = InvoiceStorage(self.postgres_dsn)` (строка 124) добавить:

```python
        from .nonce_store import NonceStore
        self.nonce_store = NonceStore(Config.REDIS_URL, Config.NONCE_TTL_SECONDS)
```

(Убедиться, что `from ..config import Config` импортирован в начале файла; если нет — добавить.)

- [ ] **Step 2: init в `initialize()`**

После `await self.invoice_storage.init()` (строка 482) добавить:

```python
        await self.nonce_store.init()
```

- [ ] **Step 3: close в `close()`**

После `await self.invoice_storage.close()` (строка 570) добавить:

```python
        await self.nonce_store.close()
```

- [ ] **Step 4: Проверить, что движок поднимается**

Run: `cd /root/rugpt && venv/bin/python -c "import asyncio; from src.engine.services.engine_service import init_engine_service, get_engine_service; asyncio.run(init_engine_service()); print(type(get_engine_service().nonce_store).__name__)"`
Expected: `NonceStore` (нужны живые Postgres+Redis)

- [ ] **Step 5: Commit**

```bash
git add src/engine/services/engine_service.py
git commit -m "feat(zt): wire NonceStore into EngineService lifecycle"
```

---

### Task 7: `WebSignatureMiddleware`

**Files:**
- Create: `src/engine/middleware/__init__.py`
- Create: `src/engine/middleware/web_signature.py`

(Тест — интеграционный в Task 9, после подключения в app.)

- [ ] **Step 1: Реализовать middleware**

Создать `src/engine/middleware/__init__.py` (пустой).

Создать `src/engine/middleware/web_signature.py`:

```python
"""WebSignatureMiddleware — Zero Trust enforcement для /api/v1/web/*.

Проверяет: allowlist → no-signature → timestamp → ECDSA-подпись (device-ключи
user_id) → route-id (шаблон роута движка) → nonce (Redis). Затем rewrite
/web→/api/v1 и форвард. Подробности — docs/superpowers/specs/2026-05-22-...
"""
import json
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..config import Config
from ..services.engine_service import get_engine_service
from ..services.crypto_service import verify_device_signature, check_timestamp
from ..services.nonce_store import NonceStoreUnavailable
from ..security.signature_payload import build_payload, SIGNATURE_FIELDS
from ..security.route_match import resolve_route_template
from ..unified_logger import get_logger

logger = get_logger("services")

_STRIP_FIELDS = {"signature", "nonce", "sig_timestamp", "route_id"}


def _suffix(path: str) -> str:
    return path[len(Config.WEB_PREFIX):] if path.startswith(Config.WEB_PREFIX) else path


def _is_allowed(path: str) -> bool:
    s = _suffix(path)
    return any(s.startswith(r) for r in Config.WEB_ALLOWED_ROUTES)


def _requires_signature(path: str) -> bool:
    s = _suffix(path)
    return not any(s.startswith(r) for r in Config.WEB_NO_SIGNATURE_ROUTES)


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


class WebSignatureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(Config.WEB_PREFIX):
            return await call_next(request)

        if not _is_allowed(path):
            return _err(404, "Route not available via /web/")

        method = request.method
        body_bytes = b""
        body_data: Dict[str, Any] = {}
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_data = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError:
                    return _err(400, "Invalid JSON body")

        if not _requires_signature(path):
            return await self._forward(request, call_next, body_data, had_body=bool(body_bytes))

        # Извлечь поля подписи
        if method == "GET":
            q = request.query_params
            user_id = q.get("user_id"); signature = q.get("signature")
            nonce = q.get("nonce"); sig_ts = q.get("sig_timestamp"); route_id = q.get("route_id")
        else:
            user_id = body_data.get("user_id"); signature = body_data.get("signature")
            nonce = body_data.get("nonce"); sig_ts = body_data.get("sig_timestamp")
            route_id = body_data.get("route_id")

        if not all([user_id, signature, nonce, route_id]) or sig_ts is None:
            return _err(401, "Missing signature data")

        try:
            timestamp = int(sig_ts)
        except (ValueError, TypeError):
            return _err(401, "Invalid sig_timestamp")

        if not check_timestamp(timestamp, Config.SIG_TIMESTAMP_TOLERANCE_SECONDS):
            return _err(401, "Request timestamp expired")

        # route-id ↔ фактический эндпоинт (по rewritten-пути)
        rewritten = Config.WEB_PREFIX.replace("/web", "")  # /api/v1
        target_path = path.replace(Config.WEB_PREFIX, "/api/v1", 1)
        matched = resolve_route_template(request.app.routes, method, target_path)
        if matched is None:
            return _err(404, "Unknown route")
        if route_id != matched:
            logger.warning("route-id mismatch", operation="web_signature",
                           signed=route_id, actual=matched, user_id=user_id)
            return _err(401, "Route mismatch")

        # Подпись
        payload = build_payload(method, route_id, body_data if method != "GET" else None,
                                nonce, timestamp)
        engine = get_engine_service()
        public_keys = await engine.device_storage.get_all_public_keys(user_id)
        if not public_keys:
            return _err(401, "No device keys registered")
        if not any(verify_device_signature(pk, payload, signature) for pk in public_keys):
            logger.warning("invalid signature", operation="web_signature", user_id=user_id)
            return _err(401, "Invalid device signature")

        # nonce-replay (fail-closed)
        try:
            fresh = await engine.nonce_store.check_and_store(user_id, nonce)
        except NonceStoreUnavailable:
            return _err(503, "Signature verifier unavailable, please retry")
        if not fresh:
            return _err(401, "Nonce already used")

        return await self._forward(request, call_next, body_data, had_body=bool(body_bytes))

    async def _forward(self, request, call_next, body_data, had_body):
        # rewrite /web → /api/v1
        request.scope["path"] = request.scope["path"].replace(Config.WEB_PREFIX, "/api/v1", 1)
        request.scope["raw_path"] = request.scope["path"].encode("ascii")

        # вырезать служебные поля из body и переотдать его роуту
        if had_body:
            clean = {k: v for k, v in body_data.items() if k not in _STRIP_FIELDS}
            clean_bytes = json.dumps(clean).encode("utf-8")

            async def receive():
                return {"type": "http.request", "body": clean_bytes, "more_body": False}

            request._receive = receive

        # вырезать служебные поля из query
        if request.query_params:
            qp = dict(request.query_params)
            if any(f in qp for f in _STRIP_FIELDS) or "user_id" in qp:
                for f in _STRIP_FIELDS:
                    qp.pop(f, None)
                request.scope["query_string"] = urlencode(qp).encode("ascii")

        return await call_next(request)
```

> Примечание: `user_id` в query для GET — служебное поле подписи; на форварде НЕ вырезаем (бизнес-роуты его читают). Поэтому в очистке query выше `user_id` оставлен (в `_STRIP_FIELDS` его нет). Для POST `user_id` остаётся в body аналогично.

- [ ] **Step 2: Проверить импорт**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.middleware.web_signature import WebSignatureMiddleware; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/engine/middleware/__init__.py src/engine/middleware/web_signature.py
git commit -m "feat(zt): WebSignatureMiddleware"
```

---

### Task 8: Подключить middleware в `app.py`

**Files:**
- Modify: `src/engine/app.py:78` (после CorrelationIDMiddleware)

Middleware подключается ПОСЛЕ Correlation/RequestLogging в коде, но Starlette исполняет последний добавленный первым «на входе». Нам нужно, чтобы проверка подписи шла внутри correlation-scope, но до бизнес-роута → добавляем после CORS.

- [ ] **Step 1: Импорт + подключение**

В `src/engine/app.py` после блока CORS (после строки 87) добавить:

```python
from .middleware.web_signature import WebSignatureMiddleware

# Zero Trust: проверка device-подписей для /api/v1/web/*
app.add_middleware(WebSignatureMiddleware)
```

- [ ] **Step 2: Проверить, что приложение импортируется**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.app import app; print('routes', len(app.routes))"`
Expected: `routes <N>` без ошибок

- [ ] **Step 3: Commit**

```bash
git add src/engine/app.py
git commit -m "feat(zt): enable WebSignatureMiddleware in app"
```

---

### Task 9: Интеграционный тест end-to-end

**Files:**
- Test: `tests/test_web_signature_middleware.py`

- [ ] **Step 1: Написать тест**

Создать `tests/test_web_signature_middleware.py`:

```python
import os, json, time, base64, uuid
import pytest, pytest_asyncio, asyncpg
from httpx import AsyncClient, ASGITransport
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.engine.app import app
from src.engine.services.engine_service import init_engine_service, get_engine_service
from src.engine.routes.auth import create_token

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost/rugpt")


def _gen_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pem


def _sign(priv, payload: str) -> str:
    der = priv.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(der).decode()


@pytest_asyncio.fixture(loop_scope="module")
async def env():
    await init_engine_service()
    engine = get_engine_service()
    pool = await asyncpg.create_pool(DSN)
    async with pool.acquire() as conn:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (id, name, slug) VALUES (gen_random_uuid(),'zt',$1) RETURNING id",
            f"zt_{uuid.uuid4().hex[:8]}")
        user_id = await conn.fetchval(
            "INSERT INTO users (id, org_id, username, name, password_hash, email, is_active) "
            "VALUES (gen_random_uuid(),$1,$2,$2,'x',$3,true) RETURNING id",
            org_id, f"u_{uuid.uuid4().hex[:6]}", f"u_{uuid.uuid4()}@t.local")
    priv, pem = _gen_keypair()
    await engine.device_storage.register_device(user_id=user_id, device_public_key=pem, device_name="t")
    token = create_token(user_id, org_id)
    await pool.close()
    return {"engine": engine, "org_id": org_id, "user_id": str(user_id),
            "priv": priv, "token": token}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio(loop_scope="module")
async def test_valid_get_passes(env):
    # GET /api/v1/web/users — реальный роут; route_id = шаблон движка
    route_id = "/api/v1/users"
    nonce = uuid.uuid4().hex; ts = int(time.time())
    payload = f"GET:{route_id}::{nonce}:{ts}"
    sig = _sign(env["priv"], payload)
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?org_id={env['org_id']}&user_id={env['user_id']}"
                        f"&signature={sig}&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 200


@pytest.mark.asyncio(loop_scope="module")
async def test_missing_signature_401(env):
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?org_id={env['org_id']}&user_id={env['user_id']}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_expired_timestamp_401(env):
    route_id = "/api/v1/users"; nonce = uuid.uuid4().hex; ts = int(time.time()) - 600
    sig = _sign(env["priv"], f"GET:{route_id}::{nonce}:{ts}")
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
                        f"&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_route_id_mismatch_401(env):
    nonce = uuid.uuid4().hex; ts = int(time.time())
    # подписываем чужой route_id, но бьём в /users
    sig = _sign(env["priv"], f"GET:/api/v1/roles::{nonce}:{ts}")
    async with _client() as c:
        r = await c.get(f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
                        f"&nonce={nonce}&sig_timestamp={ts}&route_id=/api/v1/roles",
                        headers={"Authorization": f"Bearer {env['token']}"})
    assert r.status_code == 401


@pytest.mark.asyncio(loop_scope="module")
async def test_replay_nonce_rejected(env):
    route_id = "/api/v1/users"; nonce = uuid.uuid4().hex; ts = int(time.time())
    sig = _sign(env["priv"], f"GET:{route_id}::{nonce}:{ts}")
    url = (f"/api/v1/web/users?user_id={env['user_id']}&signature={sig}"
           f"&nonce={nonce}&sig_timestamp={ts}&route_id={route_id}")
    h = {"Authorization": f"Bearer {env['token']}"}
    async with _client() as c:
        r1 = await c.get(url, headers=h)
        r2 = await c.get(url, headers=h)
    assert r1.status_code == 200
    assert r2.status_code == 401  # replay


@pytest.mark.asyncio(loop_scope="module")
async def test_disallowed_route_404(env):
    async with _client() as c:
        r = await c.get("/api/v1/web/totally-unknown")
    assert r.status_code == 404


@pytest.mark.asyncio(loop_scope="module")
async def test_no_signature_route_passes(env):
    # /config — в WEB_NO_SIGNATURE_ROUTES, подпись не нужна
    async with _client() as c:
        r = await c.get("/api/v1/web/config")
    assert r.status_code in (200, 404)  # 404 если роут /config не реализован; не 401
    assert r.status_code != 401
```

- [ ] **Step 2: Запустить**

Run: `cd /root/rugpt && venv/bin/python -m pytest tests/test_web_signature_middleware.py -v`
Expected: все passed (нужны живые Postgres+Redis). Если `test_valid_get_passes` падает на бизнес-логике (например `/users` требует доп. параметры) — заменить целевой роут на заведомо безопасный GET из allowlist, сохранив проверку статуса ≠ 401.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_signature_middleware.py
git commit -m "test(zt): end-to-end middleware enforcement"
```

---

### Task 10: Удалить `POST /auth/verify-signature`

Теперь проверку делает middleware — отдельный verify-эндпоинт не нужен.

**Files:**
- Modify: `src/engine/routes/auth.py:253-283` (класс `VerifySignatureRequest` + хендлер)

- [ ] **Step 1: Найти зависимый тест**

Run: `cd /root/rugpt && grep -rln "verify-signature\|verify_signature\|VerifySignatureRequest" tests/ src/`
Expected: список файлов (как минимум `routes/auth.py`).

- [ ] **Step 2: Удалить класс и хендлер**

В `src/engine/routes/auth.py` удалить блок: класс `VerifySignatureRequest` и функцию `verify_signature` с декоратором `@router.post("/verify-signature")` (строки ~253-283).

- [ ] **Step 3: Удалить/поправить зависимые тесты**

Если в Step 1 найдены тесты на verify-signature — удалить их (функциональность убрана осознанно).

- [ ] **Step 4: Проверить импорт приложения**

Run: `cd /root/rugpt && venv/bin/python -c "from src.engine.app import app; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add src/engine/routes/auth.py
git commit -m "refactor(zt): remove obsolete /auth/verify-signature (moved to middleware)"
```

---

### Task 11: nginx на rugpt-container — proxy `/web` без среза (деплой-шаг)

Сейчас nginx делает `rewrite ^/api/v1/web/(.*)$ /api/v1/$1` — это срезает `/web` до FastAPI, и middleware не увидит web-трафик. Нужно проксировать `/api/v1/web/*` **нетронутым**; rewrite делает middleware.

**Files:**
- Deploy: nginx-конфиг на rugpt-container (B.I.1). Выполняет пользователь на проде.

- [ ] **Step 1: Свериться с фактическим конфигом (команда пользователю)**

Дать пользователю команду для прода:

```
sudo nginx -T 2>/dev/null | grep -n "api/v1/web" 
```

- [ ] **Step 2: Целевой server-блок**

Заменить location на:

```nginx
location /api/v1/web/ {
    proxy_pass http://127.0.0.1:8100;   # БЕЗ rewrite — путь /api/v1/web/* уходит как есть
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300s;
    proxy_connect_timeout 10s;
}
location / { return 403; }
```

- [ ] **Step 3: Применить (команды пользователю)**

```
sudo nginx -t && sudo systemctl reload nginx
```

- [ ] **Step 4: Зафиксировать в доках движка**

Обновить `docs/networking.md` (раздел «Nginx на rugpt-container»): rewrite убран, `/web` срезает middleware.

- [ ] **Step 5: Commit**

```bash
git add docs/networking.md
git commit -m "docs(zt): nginx proxies /api/v1/web intact; middleware strips /web"
```

---

## Self-Review (выполнено при написании)

- **Покрытие спека:** middleware (Task 7-9), Redis-nonce (5-6), timestamp (2), route-id-binding через роут-тейбл (4,7), allowlist/no-signature (1,7), удаление verify-signature (10), nginx (11), канонизация payload (3). `org_id`/`user_id` derive — **вынесено в План B** (37 сайтов, отдельный плано). Клиентский cutover — **План C**. Multipart-подпись — **План C**.
- **Плейсхолдеры:** нет; «открытые детали» спека закрыты решениями (route-id = роут-тейбл; nonce fail-closed).
- **Согласованность типов:** `build_payload`, `SIGNATURE_FIELDS`, `resolve_route_template`, `NonceStore.check_and_store`, `NonceStoreUnavailable`, `check_timestamp` — имена едины между задачами и middleware.

## Открытые вопросы для Планов B/C

- **План B:** `org_id` (а где надо — `user_id`) выводить из проверенной личности; 37 сигнатур в 13 файлах `routes/*.py`.
- **План C:** фронт — route-id (= шаблон движка) в payload, единый реестр в `packages/common`, тест парности канонизации TS↔Python, подпись multipart (SHA-256 содержимого в payload), NestJS — прозрачное реле без трансформации тела, слать на `/api/v1/web/*`.

## Архитектурная правка (as-built)

Фактическая реализация (на dev) отличается от пошаговой структуры файлов выше — фиксируем, тело плана не переписываем.

- **Проверка вынесена в композитный сервис.** Crypto-хелперы и Redis-клиент не импортируются в middleware напрямую (как в Task 7). Вся логика проверки собрана в метод `SignatureService.verify_request_signature` (`src/engine/services/signature_service.py`, встроен в `EngineService` как `engine.signature_service`). Схема — route/middleware → service → storage: middleware делает один делегирующий вызов, сам crypto/storage не трогает (ловит только `NonceStoreUnavailable` → 503). Внутри сервиса: timestamp-окно → device-ключи юзера → ECDSA verify → nonce-replay (`NonceStore`, fail-closed).
- **Открытый вопрос «синхронизация реестра route-id TS↔Python» снят.** Отдельного Python-реестра нет. `route_id` — шаблон роута самого движка, резолвится через `resolve_route_template` против собственного роут-тейбла приложения (`app.routes`). Поддерживать в синхроне на стороне движка нечего; фронт зеркалит те же шаблоны, парность покрывается Планом C.
