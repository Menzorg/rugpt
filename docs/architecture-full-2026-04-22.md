# RuGPT — полная архитектура (для security audit)

> **Источник:** собрано 2026-04-22 итеративно, для передачи безопаснику и последующей раскладки по разделам документации.
> После раскладки этот файл можно удалить или переименовать в `architecture-archive/`.

---

## Топология — высокоуровневая

```
       ISP #1             ISP #2
          │                  │
          └────────┬─────────┘
                   ▼
    ┌──────────────────────────────────┐
    │  D. Omada ER605 (dual-WAN)       │
    │  Public: 217.113.118.218         │
    │  LAN:    192.168.1.0/24          │
    │  VPN:    10.0.0.0/24 (WireGuard) │
    └───┬────────────┬──────────┬──────┘
        │            │          │
   LAN  │       LAN  │          │ LAN
        ▼            ▼          ▼
   ┌────────┐  ┌─────────┐  ┌──────────┐
   │ B. RAG │  │ C. Zver │  │ E. NAS   │
   │ Proxmox│  │ GPU     │  │ Synology │
   │ 64/512 │  │  .80    │  │  .38     │
   └────────┘  └─────────┘  └──────────┘

   Внешние (интернет через ISP):
   ┌────────┐    ┌────────┐    ┌─────────┐
   │A. Prod │    │F. Dev- │    │G. Комп  │
   │ VPS    │    │   VPS  │    │ Алексан.│
   │rugpt.pro│   │ (AI+dev)│   │ (dev)  │
   └────┬───┘    └────┬───┘    └────┬────┘
        │WireGuard    │SSH/rsync    │git push
        │10.0.0.1     │             │(→GitLab)
        ▼             ▼             ▼
       RAG          macbook       GitLab (B.I.2)
                    ├─git push→ GitHub/GitLab
                    └─deploy.sh→ Prod
```

---

## Узел A. Prod-VPS (публичный хостинг)

**Общее:** Ubuntu 22.04 LTS, Docker Engine, публичный IP, домен `rugpt.pro`, WireGuard-пир `10.0.0.1`

| # | Сервис | Runtime | Стек | Порт | Назначение |
|---|---|---|---|---|---|
| A.1 | webclient-backend | Node.js 20-alpine (Docker) | NestJS 11, TypeScript | 4000 | REST + Socket.IO gateway |
| A.2 | webclient-frontend | Node.js 20-alpine (Docker) | Next.js 15, React 19, Zustand, Tailwind | 3000 | SSR + SPA |
| A.3 | redis | Redis 7-alpine (Docker, AOF) | — | 6379 | Sessions, presence, outbox, rate-limit, Socket.IO adapter |
| A.4 | nginx | nginx (systemd) | — | 443/80 | SSL (Let's Encrypt), reverse-proxy на A.1/A.2 |

**Backend зависимости:** `@nestjs/platform-socket.io`, `@nestjs/jwt`, `passport-jwt`, `@nestjs/throttler` (Redis), `ioredis`, `kafkajs`, `axios`, `bcrypt` (12 rounds)

**Frontend клиентская крипта:** Web Crypto API, ECDSA P-256 (non-extractable), privateKey в IndexedDB

**Rate-limit (только webclient, engine без rate-limit):**
- HTTP Throttler: default 100/min, strict 10/min, ключ `webchat:throttle:{name}:{userId}:{route}`
- WS RedisWsThrottlerGuard: 100/min/event/user

**Socket.IO:** `pingInterval=10s`, `pingTimeout=30s`, only `websocket`, комнаты `user:<userId>`, `chat:<chatId>`

**CORS:** `origin=FRONTEND_URL`, `credentials: true`

**MAINTENANCE_USERS** (`.docker.env`): `email:password,...`, при maintenance mode bcrypt-проверяется локально, минуя engine

---

## Узел B. RAG (Proxmox VE)

**Железо:** 64 cores (2 × 32), 512 GB RAM
**Внешний IP:** 217.113.118.218
**WireGuard-пир:** 10.0.0.2

### B.I. LXC

| # | Имя | ОС | Стек | CPU pin | RAM | Диск | LAN IP |
|---|---|---|---|---|---|---|---|
| B.I.1 | rugpt-container | Ubuntu 22.04 | Python 3.10+, FastAPI, Uvicorn, LangChain, LangGraph, nginx reverse-proxy | 50-52 (3) | 10 GB | 80 GB | **192.168.1.81** |
| B.I.2 | gitlab-container | Ubuntu 24.04 | GitLab CE Omnibus (Rails + PG + Redis + Nginx + Gitaly + Sidekiq) | 53-56 (4) | 8 GB | 150 GB | **192.168.1.118** |
| B.I.3+ | (TBD) сторонние проекты | — | LXC для бэкендов бизнес-проектов | из резерва | TBD | TBD | TBD |

**rugpt-container — подсистемы (все в одном uvicorn):**
- FastAPI :8100 + nginx reverse-proxy (`/api/v1/web/*` → FastAPI, всё прочее → 403)
- SchedulerService (asyncio task, poll 30s) — calendar + per-org morning/evening jobs через ZoneInfo
- KafkaConsumerLoop(agent.requests) (background asyncio) — at-least-once
- IngestQueue (ThreadPoolExecutor, 3 воркера, отдельные asyncpg-пулы) — RAG-индексация
- Зависимости: `asyncpg` (pool 2-10), `langchain`, `langgraph`, `langchain-openai`, `aiokafka`, `PyJWT`, `bcrypt`, `cryptography`, `croniter`, `aiosmtplib`, `httpx`

**gitlab-container:** SSH :22 → NAT `217.113.118.218:28351`, юзер `workeradmin`. Миграция из GitHub после запуска.

### B.II. VM (KVM)

| # | Имя | ОС | Сервисы | CPU pin | RAM | Диск | LAN IP |
|---|---|---|---|---|---|---|---|
| B.II.1 | postgres-vm | Ubuntu 22.04 | PostgreSQL 16 + pgvector | 0-34 (35) | 368 GB | 200 + 2000 GB | **192.168.1.82** |
| B.II.2 | docker-vm | Ubuntu 22.04 + Docker | Kafka 3.7, Tika, n8n, сторонние проекты | 35-49 (15) | 70 GB | 1 TB | **192.168.1.84** |
| B.II.3 | gitlab-runner-vm | Ubuntu 24.04 + Docker | `gitlab/gitlab-runner` (Docker executor) | 57-60 (4) | 4 GB | 50 GB | **192.168.1.119** |
| B.II.4 | dev-vm | Ubuntu 22.04 | PostgreSQL + Redis + Kafka + FastAPI (engine копия) + NestJS + Next.js — всё локально | 4 (из резерва) | 32 GB | 200 GB | TBD |

**postgres-vm:**
- PostgreSQL 16 + pgvector + pgcrypto
- Auth: scram-sha-256 / md5
- pgvector — **одна общая схема** (не per-role), изоляция `org_id AND (is_public OR user_id = viewer)`
- Vector dim: 1024, HNSW индексы
- Бэкап: `pg_dump` → NAS + Proxmox snapshot

**docker-vm (multi-tenant):**
- Apache Kafka 3.7 (bitnami, KRaft, без ZK) — :9092
  - `agent.requests`: 3 partitions, retention 24h, idempotent, `acks=all`, partition key = request_id
  - `chat.events`: 3 partitions, retention 1h, partition key = chat_id
  - Auth: **отсутствует** (trust by VPN; план SASL_PLAINTEXT после Alpha)
- Apache Tika Server (Java) — :9998
- n8n (план)
- Сторонние проекты владельца

**gitlab-runner-vm:**
- `gitlab/gitlab-runner:latest`, executor=docker, mount `/var/run/docker.sock` (trusted-only), `concurrent=1`
- Рекомендация: kaniko/buildkit вместо dind
- SSH :22 → NAT `217.113.118.218:24952`, юзер `workeradmin`

**dev-vm (изолированная среда разработки):**
- Полная all-in-one копия prod-стека на одной KVM-VM
- PG — локальный, с клоном prod-данных после анонимизации
- Auto-deploy из GitLab CI при push в ветку `dev`
- Snapshot Proxmox → rollback при поломке

**Резерв после выделения dev-vm:** ~0-1 cores + 20 GB RAM

---

## Узел C. Zver (192.168.1.80) — LLM-хост

- Физ. сервер с GPU (зона Александра), Ubuntu + CUDA

| # | Сервис | Runtime | Порт | Auth |
|---|---|---|---|---|
| C.1 | LiteLLM proxy | Python | 4000 | Bearer `sk-dummy` (любой непустой токен) |
| C.2 | vLLM | Python + CUDA | локально | — |

**Модели:** `google/gemma-4-31B-it` (генерация), `Qwen/Qwen3-Embedding-0.6B` (1024-dim)

**URL engine→:** `http://192.168.1.80:4000/v1`
**API:** OpenAI-compatible
**Health:** `GET /health/liveness` без auth

---

## Узел D. Omada Gateway ER605

- TP-Link Omada ER605, **dual-WAN** (ISP #1 + ISP #2)
- Public IP: 217.113.118.218
- LAN: 192.168.1.0/24 (DHCP), WireGuard-сервер: 10.0.0.0/24
  - Prod-VPS = 10.0.0.1, RAG = 10.0.0.2
- NAT port-forward: `:28351` → `192.168.1.118:22`, `:24952` → `192.168.1.119:22`
- ACL: VPN-пир Prod-VPS → только 10.0.0.2

---

## Узел E. NAS (Synology)

- DSM 7.3.2-86009 Update 3
- Btrfs на RAID 5, IP **192.168.1.38**
- Подключение к Proxmox: NFS (уточнить)
- Режим **WriteOnce** (immutable snapshots)
- Содержимое: `vzdump` + PG-dumps

---

## Узел F. Dev-VPS

- Ubuntu, отдельный публичный IP, SSH
- `/root/rugpt/`, `/root/webclient_rugpt/` — код без `.git`, без `.env`, без `venv/node_modules`
- Скрипты: `sync.sh` (dev-VPS → macbook), `deploy.sh` (macbook → prod)
- Минимизация blast-radius: компрометация = только код snapshot, без истории и секретов

---

## Узел G. Рабочий компьютер Александра

- Второй разработчик, свой комп
- Push в `feature/*`, `dev` → GitHub/GitLab
- SSH-ключи, VPN к офису (опц.)

---

## Внешние интеграции

| Сервис | Протокол | Направление | Auth | Статус |
|---|---|---|---|---|
| Telegram Bot API | HTTPS | Engine → `api.telegram.org/sendMessage` | Bot token (.env) | Работает |
| Telegram webhook | HTTPS | TG → Engine `/notifications/telegram/webhook` | без JWT | **Недоступен** (за VPN) |
| SMTP | TCP/TLS | Engine → SMTP | login/password (.env) | Конфиг в .env |
| GitHub | HTTPS | Разработчики → GitHub | SSH-keys/PAT | Мигрирует в GitLab |
| OAuth (Google/Я/VK/Mail/TG) | — | — | — | **Не реализовано** |
| Perplexity / web_search | — | — | — | **Stub** |
| `role_call` tool | — | — | — | **Stub** |

---

## Файловое хранилище

- Бинарники: `/root/rugpt/uploads/{org_id}/{user_id}/{file_id}.{ext}` в rugpt-container
- Метаданные: PG таблица `user_files`
- Дедупликация: SHA-256 per-user
- `STORAGE_BACKEND=local`

---

## Сетевые потоки

| Источник | Назначение | Протокол/Порт | Шифрование | Auth |
|---|---|---|---|---|
| Интернет | A.4 nginx | HTTPS :443 | TLS 1.2/1.3 | — |
| A.4 | A.1/A.2 | HTTP loopback | нет | — |
| A.1 | A.3 Redis | TCP :6379 | нет | password (prod) |
| A.1 | B.I.1 Engine | HTTP | WireGuard ChaCha20 | JWT + ECDSA signature |
| A.1 (Kafka) | B.II.2 Kafka | TCP :9092 | WireGuard | **нет** |
| B.I.1 | B.II.1 PG | TCP :5432 | нет (LAN) | scram-sha-256 |
| B.I.1 | B.II.2 Kafka | TCP :9092 | нет | **нет** |
| B.I.1 | B.II.2 Tika | HTTP :9998 | нет | **нет** |
| B.I.1 | C.1 LiteLLM | HTTP :4000 | нет | Bearer `sk-dummy` |
| B.I.1 | api.telegram.org | HTTPS | TLS | Bot token |
| B.I.1 | SMTP | TCP/TLS | TLS | login/password |
| GitLab CI | Prod (A, B.I.1) | SSH | SSH | keys в CI Variables |
| Proxmox | E. NAS | NFS | нет | UID mapping |
| Админ | B.I.2 SSH | :22 | SSH | keys |
| Админ | B.II.3 SSH | :22 | SSH | keys |
| F. Dev-VPS | macbook | SSH rsync | SSH | keys |
| Macbook | Prod VPS, RAG | SSH rsync | SSH | keys |

---

## Zero-Trust — развёрнутый флоу

### Принцип
JWT — bearer token (украл = юзер). ECDSA P-256 device-signature добавляет второй фактор: каждый mutation подписан приватным ключом устройства. Публичный ключ хранится в engine `user_devices`.

### Шаг 0 — регистрация устройства (login)
```
Browser:
  crypto.subtle.generateKey('ECDSA', P-256, extractable=false)
  → {privateKey, publicKey}
  privateKey → IndexedDB (non-extractable)
  publicKey → PEM
           │
           │ POST /api/auth/login {email, pwd, devicePublicKey}
           ▼
NestJS → Engine /api/v1/auth/login
           │
           ▼
Engine:
  verify_password (bcrypt 12 rounds)
  device_storage.register_device(...)
  → INSERT user_devices
  create_token(...) → JWT (HMAC-SHA256)
           │
           ▼
Browser: token в Zustand, privateKey в IndexedDB
```

### Шаг 1 — подписанный запрос
```
Browser apiClient.signedGet/Post/...:
  nonce = random(16).hex()
  sig_timestamp = now()
  payload = canonical({path, body?, nonce, sig_timestamp, user_id})
  signature = ECDSA.sign(SHA256(payload), privateKey)
  
  → GET /api/users?signature=...&nonce=...&sig_timestamp=...&user_id=...
    Header: Authorization: Bearer <JWT>
           │
           ▼
NestJS SignatureGuard:
  if @SkipSignature: skip
  else POST engine /auth/verify-signature {user_id, payload, signature}
           │
           ▼
Engine verify_signature:
  devices = list_by_user(user_id)
  for device: ECDSA.verify(signature, SHA256(payload), device.public_key)
  check |now - sig_timestamp| < 5min
  check nonce not in TTL-cache
  → 200 / 401
           │
           ▼
NestJS JwtAuthGuard (локальная проверка JWT)
           │
           ▼
Controller → Service → engineAdapter.execute()
  → HTTP к engine с Authorization: Bearer <JWT>
Engine валидирует JWT ещё раз → обрабатывает запрос
```

### Шаг 2 — атака: украденный JWT
```
Атакующий с JWT без signature:
  GET /api/users   (нет signature/nonce/timestamp)
    Auth: Bearer <stolen JWT>
           │
           ▼
NestJS SignatureGuard: "Missing signature data" → 401

Атакующий НЕ МОЖЕТ подписать payload:
  privateKey в IndexedDB жертвы, non-extractable,
  не достаётся даже через DevTools.

Доступны только @SkipSignature() endpoints:
  /auth/login, /auth/register, /config, /health*,
  /files/upload (legacy!), /files/:id/download (legacy!),
  /notifications/telegram/webhook
```

### Шаг 3 — матрица защиты

| Threat | Защита |
|---|---|
| Кража JWT (XSS, cookie) | SignatureGuard блокирует без device key |
| Кража privateKey | Non-extractable ECDSA — не выносится |
| MITM публичный | TLS 1.2/1.3 на nginx |
| MITM в VPN/LAN | WireGuard ChaCha20 |
| Replay | timestamp ±5min + nonce cache |
| Compromise NestJS | Не имеет private key, не подписывает |
| Compromise Engine | Видит publickey + JWT_SECRET → total loss |
| Brute-force ECDSA | 128-bit security, непрактично |

### Шаг 4 — слабые места
1. WebSocket не подписывается (только JWT handshake) — после установки все events доверяются
2. File upload/download без signature (`@SkipSignature`) — legacy
3. Один JWT_SECRET на webclient + engine — compromise одного = обоих
4. Нет admin revoke device endpoint
5. Nonce-cache не persistent — replay-window при рестарте engine
6. Engine compromise = total compromise всего

---

## Криптография и секреты

| Уровень | Механизм |
|---|---|
| TLS наружу | nginx + Let's Encrypt (TLS 1.2/1.3) |
| TLS внутри LAN | **отсутствует** |
| VPN | WireGuard ChaCha20-Poly1305 |
| App-layer | JWT HMAC-SHA256 + ECDSA P-256 signatures |
| Пароли | bcrypt 12 rounds |
| Secret storage | `.env` plaintext (engine, webclient, GitLab, LiteLLM) |
| Централизованный vault | **нет** |
| Ротация секретов | **нет** |

---

## Пользователи и доступы

| Лицо | Роль | Доступ |
|---|---|---|
| Petr | lead dev | dev-VPS, macbook → prod SSH, GitHub/GitLab |
| Alexander (dev) | второй dev | свой комп, GitLab (после миграции), VPN |
| Alexander (infra) | LLM/Zver | физический + SSH |
| workeradmin | сервисный | gitlab-container, runner-vm SSH |
| Eduard | владелец | GitLab viewer, dashboard |

---

## Публичные эндпоинты (без JWT)

- `/auth/login`, `/auth/register`, `/auth/verify-signature`
- `/config` (maintenance)
- `/health*`
- `/notifications/telegram/webhook`
- `/files/upload`, `/files/:id/download` — legacy
- Engine CORS `allow_origins=["*"]` — dev-настройка!

---

## Workflow разработки

### Текущий
```
dev-VPS (F) → sync.sh → macbook ~/rugpt/ → deploy.sh → prod
                       (git push → GitHub)
```

### Планируемый (после GitLab миграции)
```
разработчик (F, G, macbook) → git push origin dev
  → GitLab CI (runner-vm B.II.3)
  → build/test/deploy → dev-vm (B.II.4)
  → ручное тестирование
  → MR dev → main, approval
  → CI → deploy prod (A, B.I.1)
```

---

## Фоновые процессы / scheduler

Всё в одном uvicorn rugpt-container через asyncio.create_task:
- SchedulerService (30s polling, calendar + per-org timezone task jobs)
- KafkaConsumerLoop(agent.requests)
- IngestQueue (ThreadPoolExecutor 3 воркера, отдельные asyncpg-пулы)

---

## Логи и мониторинг

| Компонент | Логи | Мониторинг |
|---|---|---|
| Engine | `/root/rugpt/logs/` файлы | нет |
| Webclient | `docker logs`, планируется nestjs-pino daily-dir | нет |
| PG | на postgres-vm | нет |
| Kafka | `docker logs` на docker-vm | нет |
| Sentry/Prometheus/Loki/ELK | — | **не внедрены** |

---

## Security-аудит — точки внимания

1. TLS отсутствует в LAN — всё plaintext между сервисами
2. Один JWT_SECRET на webclient + engine
3. Нет secret management (plaintext `.env`, без rotation)
4. CORS Engine = `*` (dev в prod)
5. LiteLLM auth = `sk-dummy`
6. Kafka без SASL/TLS — trust by VPN
7. Redis без password в dev (проверить prod)
8. docker-vm multi-tenant — prod Kafka/Tika + n8n + сторонние
9. docker.sock mounted в gitlab-runner — побег из CI
10. Dev-среда с клоном prod-данных — нужна scripted анонимизация
11. Kernel sharing на Proxmox LXC — kernel-CVE компрометирует всё
12. Публичные SSH через NAT — брутфорс-цель
13. Dual-WAN failover — ACL консистентны на обоих ISP
14. Apache Tika (Java) — CVE-prone
15. NAS WriteOnce + RAID 5 — single NAS = SPOF
16. File upload/download без signature — legacy
17. WebSocket без подписи — только JWT
18. Нет admin device revoke
19. MAINTENANCE_USERS в `.docker.env` — байпас при утечке
20. Engine без rate-limit — открыт в VPN
21. Nonce-cache не persistent — replay-window при рестарте
22. Telegram webhook недоступен из-за VPN
23. GitHub→GitLab миграция — план отозвать токены
24. Нет signed commits/artifacts — добавить в GitLab CI
25. SSH-keys macbook — единая точка для prod; HW key рекомендуется

---

## Технологический стек

| Слой | Стек |
|---|---|
| Frontend (A.2) | Next.js 15, React 19, TypeScript, Zustand, Tailwind, Socket.IO-client, Web Crypto API ECDSA P-256 |
| Backend webclient (A.1) | NestJS 11, TypeScript, Node 20-alpine, Socket.IO, ioredis, kafkajs, passport-jwt, bcrypt |
| Engine (B.I.1) | Python 3.10+, FastAPI, Pydantic v2, asyncpg, LangChain, LangGraph, aiokafka, PyJWT, cryptography, croniter, aiosmtplib, httpx |
| БД (B.II.1) | PostgreSQL 16 + pgvector + pgcrypto |
| Kafka (B.II.2) | Apache Kafka 3.7 (bitnami, KRaft) |
| PDF/DOCX (B.II.2) | Apache Tika Server (Java) |
| Автоматизация | n8n (план) |
| LLM gateway (C.1) | LiteLLM (Python) |
| LLM inference (C.2) | vLLM + CUDA |
| Кеш (A.3) | Redis 7-alpine, AOF |
| VCS | GitHub → GitLab CE Omnibus (B.I.2) |
| CI/CD | GitLab Runner Docker executor (B.II.3) |
| Бэкап | Proxmox vzdump → Synology NAS (Btrfs RAID 5 WriteOnce) |
| VPN | WireGuard |
| SSL | nginx + Let's Encrypt (VPS) |
| Deploy | rsync/SSH (текущ.) → GitLab CI (план) |
