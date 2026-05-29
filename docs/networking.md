# RuGPT Engine: Сетевая архитектура

> Логическая схема + app-layer auth. Полная физическая топология, CPU pinning, IP-карта и 25 security-пунктов — `architecture-full-2026-04-22.md` и `architecture-2026-04-22.drawio`.

## Топология

```
          ISP #1          ISP #2
             │              │
             └──────┬───────┘
                    ▼
       ┌───────────────────────────────┐
       │  D. Omada ER605 (dual-WAN)    │
       │  public: 217.113.118.218      │
       │  LAN:    192.168.1.0/24       │
       │  VPN:    10.0.0.0/24 (WG)     │
       └───┬───────────┬───────────┬───┘
           │LAN        │LAN        │LAN
           ▼           ▼           ▼
      ┌────────┐  ┌────────┐  ┌────────┐
      │B. RAG  │  │C. Zver │  │E. NAS  │
      │Proxmox │  │ GPU    │  │Synology│
      │  .81+  │  │  .80   │  │  .38   │
      └────────┘  └────────┘  └────────┘

          internet (HTTPS)         WireGuard
       ┌──────────────────┐   ┌─────────────────┐
       │ A. Prod-VPS      │   │ tunnel          │
       │ rugpt.pro        │◄──┤10.0.0.1↔10.0.0.2│
       │ WG peer 10.0.0.1 │   │ ChaCha20-Poly   │
       └──────────────────┘   └─────────────────┘

       ┌──────────┐   SSH rsync    ┌──────────┐
       │F. Dev-VPS│───────────────▶│ Macbook  │
       └──────────┘                └────┬─────┘
                                        │ deploy.sh SSH rsync
       ┌──────────┐   git push           ▼ (к A + rugpt-container)
       │G. Alex PC│──────▶ GitHub / GitLab CE (B.I.2, NAT :28351)
       └──────────┘
```

Engine не открыт в интернет — только через WireGuard 10.0.0.0/24. На rugpt-container перед FastAPI стоит nginx, фильтрующий всё кроме префикса `/api/v1/web/*`. Движок зависит от собственного Redis (`REDIS_URL`, по умолчанию `redis://localhost:6379/0`) для nonce/replay-защиты — без доступного Redis подписанные запросы отклоняются (fail-closed, см. ниже).

## VPN (WireGuard 10.0.0.0/24)

| Хост | WG IP | Роль |
|---|---|---|
| A. Prod-VPS | **10.0.0.1** | Тонкий proxy webclient → engine |
| B. RAG Proxmox (rugpt-container) | **10.0.0.2** | Engine FastAPI |

- **Сервер WG** — Omada ER605 (D).
- **Шифрование** — ChaCha20-Poly1305.
- **ACL на Omada**: VPN-пир Prod-VPS может обращаться только к `10.0.0.2`, остальной LAN закрыт для VPN.
- **Dual-WAN failover** (ISP #1 + ISP #2) — при деплое ACL **должны** быть консистентны на обоих ISP (security-пункт 13).

## NAT port-forwards на Omada

| Public | LAN target | Юзер |
|---|---|---|
| `217.113.118.218:28351` | `192.168.1.118:22` (GitLab SSH) | `workeradmin` |
| `217.113.118.218:24952` | `192.168.1.119:22` (Runner SSH) | `workeradmin` |

Публичные SSH-порты — brute-force target (security-пункт 12). Рекомендуется fail2ban + только ключ-based auth.

## Карта IP (LAN 192.168.1.0/24)

| IP | Хост | Назначение |
|---|---|---|
| `.38` | NAS Synology (E) | Бэкапы vzdump + pg_dump |
| `.80` | Zver (C) | LiteLLM + vLLM |
| `.81` | rugpt-container (B.I.1) | Engine FastAPI + nginx + Redis (nonce/replay) |
| `.82` | postgres-vm (B.II.1) | PostgreSQL 16 + pgvector |
| `.84` | docker-vm (B.II.2) | Kafka + Tika + n8n |
| `.118` | gitlab-container (B.I.2) | GitLab CE Omnibus |
| `.119` | gitlab-runner-vm (B.II.3) | GitLab Runner (Docker) |

Redis движка живёт на самом rugpt-container (loopback `127.0.0.1:6379`), отдельно от Redis вебклиента на Prod-VPS.

## Сетевые потоки

| Источник | Назначение | Протокол/Порт | Шифрование | Auth |
|---|---|---|---|---|
| Интернет (пользователь) | A.4 nginx (Prod-VPS) | HTTPS :443 | TLS 1.2/1.3 | — |
| A.4 nginx | A.1 NestJS | HTTP loopback :4000 | нет | — |
| A.4 nginx | A.2 Next.js | HTTP loopback :3000 | нет | — |
| A.1 NestJS | A.3 Redis (вебклиент) | TCP :6379 | нет | password (prod) |
| A.1 NestJS | **B.I.1 Engine** (через WG) | HTTP → `10.0.0.2/api/v1/web/*` | WireGuard ChaCha20 | JWT + ECDSA signature |
| A.1 NestJS | B.II.2 Kafka (через WG→LAN) | TCP :9092 | WireGuard | **нет** |
| B.I.1 Engine | B.I.1 Redis (nonce/replay) | TCP loopback :6379 | нет | нет (loopback) |
| B.I.1 Engine | B.II.1 PostgreSQL | TCP :5432 | нет (LAN) | scram-sha-256 |
| B.I.1 Engine | B.II.2 Kafka | TCP :9092 | нет | **нет** (trust by LAN) |
| B.I.1 Engine | B.II.2 Tika | HTTP :9998 | нет | **нет** |
| B.I.1 Engine | C.1 LiteLLM | HTTP :4000 | нет | `Bearer sk-dummy` |
| B.I.1 Engine | `api.telegram.org` | HTTPS | TLS | Bot token (исходящий только) |
| B.I.1 Engine | SMTP | TCP/TLS | TLS | login/password |
| GitLab CI | Prod-VPS (A) + rugpt-container (B.I.1) | SSH | SSH | keys в CI variables |
| Proxmox | NAS Synology (E) | NFS | нет | UID mapping |
| Macbook | Prod-VPS, rugpt-container | SSH rsync | SSH | keys |
| F. Dev-VPS | Macbook | SSH rsync | SSH | keys |

## Nginx на rugpt-container

Nginx-фильтр на `10.0.0.2:80` проксирует префикс `/api/v1/web/*` на FastAPI **без изменений** — срезание `/web` теперь делает движок (`WebSignatureMiddleware`) после проверки подписи:

```nginx
server {
    listen 80;
    server_name 10.0.0.2;

    location /api/v1/web/ {
        proxy_pass http://127.0.0.1:8100;   # БЕЗ rewrite — путь /api/v1/web/* уходит в FastAPI как есть
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;   # LLM генерация может занять минуты
        proxy_connect_timeout 10s;
    }

    location / {
        return 403;
    }
}
```

### Роль nginx

1. **Фильтр префикса** — гарантия что запрос пришёл от webclient через VPN (а не случайный из 10.0.0.0/24).
2. **Проксирование без rewrite** — nginx отдаёт `/api/v1/web/*` в FastAPI как есть, префикс `/web` **не** срезается. Срезание делает движок: `WebSignatureMiddleware` видит префикс `/api/v1/web/*`, по нему понимает что это web-трафик, проверяет device-подпись и только после успешной проверки переписывает `/web` → `/api/v1` внутри приложения.
3. **403** — всё, что не подходит под префикс.
4. **Timeout 300s** — для долгих LLM-запросов (synchronous режим без Kafka).

## Маппинг роутов

WebClient шлёт на `/api/v1/web/*`. Префикс `/web` теперь срезает **движок** (`WebSignatureMiddleware` после проверки подписи), а не nginx — nginx проксирует путь без изменений. Через web доступны только группы из `WEB_ALLOWED_ROUTES`; всё остальное движок отдаёт 404.

`WEB_ALLOWED_ROUTES` (config.py): `/auth`, `/users`, `/roles`, `/chats`, `/organizations`, `/calendar`, `/notifications`, `/in-app-notifications`, `/tasks`, `/task-polls`, `/task-reports`, `/projects`, `/files`, `/folders`, `/rag`, `/departments`, `/support`, `/corrections`, `/actions`, `/invoices`, `/config`, `/health`.

| WebClient запрос | FastAPI роут |
|---|---|
| `POST /api/v1/web/auth/login` | `/api/v1/auth/login` |
| `GET  /api/v1/web/organizations` | `/api/v1/organizations` |
| `GET  /api/v1/web/users` | `/api/v1/users` |
| `GET  /api/v1/web/roles` | `/api/v1/roles` |
| `GET  /api/v1/web/chats/my` | `/api/v1/chats/my` |
| `POST /api/v1/web/chats/{id}/messages` | `/api/v1/chats/{id}/messages` |
| `GET  /api/v1/web/calendar/events` | `/api/v1/calendar/events` |
| `GET  /api/v1/web/notifications/channels` | `/api/v1/notifications/channels` |
| `GET  /api/v1/web/in-app-notifications` | `/api/v1/in-app-notifications` |
| `GET  /api/v1/web/tasks` | `/api/v1/tasks` |
| `GET  /api/v1/web/task-polls/today` | `/api/v1/task-polls/today` |
| `GET  /api/v1/web/task-reports` | `/api/v1/task-reports` |
| `GET  /api/v1/web/projects` | `/api/v1/projects` |
| `POST /api/v1/web/files/upload` | `/api/v1/files/upload` |
| `GET  /api/v1/web/folders` | `/api/v1/folders` |
| `GET  /api/v1/web/rag/docs/find` | `/api/v1/rag/docs/find` |
| `GET  /api/v1/web/support/...` | `/api/v1/support/...` |
| `POST /api/v1/web/corrections` | `/api/v1/corrections` |
| `GET  /api/v1/web/actions` | `/api/v1/actions` |
| `GET  /api/v1/web/invoices` | `/api/v1/invoices` |
| `GET  /api/v1/web/health` | `/api/v1/health` |

## App-layer auth (Zero-Trust)

Двухслойная аутентификация поверх WireGuard:

1. **JWT HMAC-SHA256** — создаётся engine'ом после `POST /auth/login`. Хранится в Zustand браузера.
2. **ECDSA P-256 device signature** — приватный ключ non-extractable в IndexedDB браузера (Web Crypto API `generateKey('ECDSA', P-256, extractable=false)`). Публичный ключ хранится в engine `user_devices`.

Флоу запроса с подписью:

```
Browser apiClient.signedPost/Get/...:
  nonce = random(16).hex()
  sig_timestamp = now()
  payload = canonical({path, body?, nonce, sig_timestamp, user_id})
  signature = ECDSA.sign(SHA256(payload), privateKey)

  → GET /api/v1/web/users?signature=...&nonce=...&sig_timestamp=...&user_id=...&route_id=...
    Authorization: Bearer <JWT>

NestJS SignatureGuard:
  если route помечен @SkipSignature → пропустить
  иначе — добавить signature-поля в запрос, проксировать к движку

Движок WebSignatureMiddleware (на /api/v1/web/*):
  if path ∈ WEB_NO_SIGNATURE_ROUTES → пропустить без подписи
  else delegate → signature_service.verify_request_signature:
    check_timestamp: |now − sig_timestamp| ≤ 300 сек
    list user_devices → ECDSA.verify по каждому ключу
    nonce check_and_store (Redis SET NX EX) — replay-защита, последним шагом
    NonceStoreUnavailable (Redis недоступен) → 503
  при успехе → request.state.zt_user_id, переписать /web → /api/v1
  → Controller → Service

Движок отдельно валидирует JWT для определения текущего пользователя.
```

Проверку подписи делает **движок** в `WebSignatureMiddleware`, делегируя `signature_service.verify_request_signature`. Отдельного эндпоинта `/auth/verify-signature` в движке **нет**.

Детали атак и threat matrix — `architecture-full-2026-04-22.md` раздел «Zero-Trust развёрнутый флоу».

### Multipart (file upload) — тоже подписывается

Загрузка файлов через `multipart/form-data` **требует** подписи (отдельная ветка в middleware). Подписываемое тело — канонический JSON из текстовых form-полей (минус signature-поля) плюс `file_sha256` (sha256 байт файла). Сырые байты multipart переотдаются downstream нетронутыми. То есть `/files/...` НЕ в no-signature allowlist движка.

### Публичные endpoints движка (без подписи)

Не требуют ECDSA-подписи — список `WEB_NO_SIGNATURE_ROUTES` (config.py):

- `/auth/login`
- `/auth/engine-public-key`
- `/config`
- `/health`
- `/notifications/telegram/webhook`

Это allowlist **движка** (проверяется `_requires_signature` в `WebSignatureMiddleware`). Не путать с декораторами `@SkipSignature` на стороне NestJS — это отдельный, NestJS-side механизм пропуска guard'а; их списки могут не совпадать. В движке загрузка/скачивание файлов подпись **требуют** (см. multipart-ветку выше).

**WebSocket** подписи **нет** — только JWT на handshake (security-пункт 17).

## FastAPI bind

```env
API_HOST=127.0.0.1
API_PORT=8100
```

FastAPI слушает **только localhost** внутри rugpt-container. Единственный внешний путь — через nginx на `10.0.0.2:80` (WireGuard).

## Параметры подписи

| Параметр | Значение | Источник |
|---|---|---|
| Окно timestamp | ±300 сек (±5 мин) | `SIG_TIMESTAMP_TOLERANCE_SECONDS` |
| TTL nonce | 300 сек | `NONCE_TTL_SECONDS` |
| Хранилище nonce | Redis, ключ `nonce:{user_id}:{nonce}`, `SET NX EX` | `REDIS_URL` |
| Поведение при отказе Redis | fail-closed → HTTP 503 | `NonceStoreUnavailable` |
| Алгоритм подписи | ECDSA P-256 / SHA-256 | — |

## Безопасность — краткое резюме

| Граница | Защита |
|---|---|
| Интернет → Prod-VPS | TLS 1.2/1.3 (nginx + Let's Encrypt) |
| Prod-VPS ↔ Engine | WireGuard ChaCha20-Poly1305 |
| Engine LAN (PG/Kafka/Tika/LiteLLM) | plaintext, trust by LAN. **TLS отсутствует** (security-пункт 1) |
| App-layer запросы (mutation + GET через web) | JWT + ECDSA signature + nonce-replay в Redis |
| File upload/download | ECDSA-подпись (multipart-ветка, не legacy-skip) |
| WebSocket | только JWT (без signature — известный gap) |
| CORS Engine | `allow_origins=["*"]`, `allow_methods=["*"]` — dev-настройка в prod (security-пункт 4) |
| Nonce-store недоступен | fail-closed → 503, запрос не пропускается |

### Известные замечания

- **Telegram webhook** (`POST /api/v1/notifications/telegram/webhook`) **недоступен извне** — engine за VPN. Исходящий вызов `Engine → api.telegram.org` работает. Для входящего webhook нужен отдельный proxy через Prod-VPS (не реализовано).
- **Engine без rate-limit** — открыт в VPN без throttling (security-пункт 20). Rate-limit — только на webclient.
- **Nonce-replay в Redis, fail-closed** (security-пункт 21): nonce хранятся в Redis движка (`nonce:{user_id}:{nonce}`, TTL `NONCE_TTL_SECONDS`), а не in-memory. Они **переживают перезапуск** движка, поэтому replay-окна на рестарте **не** возникает. При недоступности Redis `NonceStore.check_and_store` кидает `NonceStoreUnavailable`, и middleware возвращает **503** «Signature verifier unavailable» — запрос НЕ пропускается. Цена fail-closed: падение Redis = недоступность всех подписанных запросов (Redis — критичная зависимость наравне с PostgreSQL).
- **Один JWT_SECRET** общий на webclient + engine — compromise одного = обоих (security-пункт 2).

Полный список — `tech-debt.md` раздел «Infrastructure & Security».
