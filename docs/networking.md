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

Engine не открыт в интернет — только через WireGuard 10.0.0.0/24. На rugpt-container перед FastAPI стоит nginx, фильтрующий всё кроме префикса `/api/v1/web/*`.

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
| `.81` | rugpt-container (B.I.1) | Engine FastAPI + nginx |
| `.82` | postgres-vm (B.II.1) | PostgreSQL 16 + pgvector |
| `.84` | docker-vm (B.II.2) | Kafka + Tika + n8n |
| `.118` | gitlab-container (B.I.2) | GitLab CE Omnibus |
| `.119` | gitlab-runner-vm (B.II.3) | GitLab Runner (Docker) |

## Сетевые потоки

| Источник | Назначение | Протокол/Порт | Шифрование | Auth |
|---|---|---|---|---|
| Интернет (пользователь) | A.4 nginx (Prod-VPS) | HTTPS :443 | TLS 1.2/1.3 | — |
| A.4 nginx | A.1 NestJS | HTTP loopback :4000 | нет | — |
| A.4 nginx | A.2 Next.js | HTTP loopback :3000 | нет | — |
| A.1 NestJS | A.3 Redis | TCP :6379 | нет | password (prod) |
| A.1 NestJS | **B.I.1 Engine** (через WG) | HTTP → `10.0.0.2/api/v1/web/*` | WireGuard ChaCha20 | JWT + ECDSA signature |
| A.1 NestJS | B.II.2 Kafka (через WG→LAN) | TCP :9092 | WireGuard | **нет** |
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

Nginx-фильтр на `10.0.0.2:80` срезает префикс `/web` и проксирует на FastAPI:

```nginx
server {
    listen 80;
    server_name 10.0.0.2;

    location /api/v1/web/ {
        rewrite ^/api/v1/web/(.*)$ /api/v1/$1 break;
        proxy_pass http://127.0.0.1:8100;
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
2. **Rewrite** — убирает `/web` перед проксированием.
3. **403** — всё, что не подходит под префикс.
4. **Timeout 300s** — для долгих LLM-запросов (synchronous режим без Kafka).

## Маппинг роутов

WebClient шлёт на `/api/v1/web/*`, nginx переписывает на `/api/v1/*`:

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
| `GET  /api/v1/web/rag/docs/find` | `/api/v1/rag/docs/find` |
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

  → GET /api/users?signature=...&nonce=...&sig_timestamp=...&user_id=...
    Authorization: Bearer <JWT>

NestJS SignatureGuard:
  if @SkipSignature → skip
  else POST engine /auth/verify-signature {user_id, payload, signature}

Engine verify_signature:
  list user_devices → ECDSA.verify per device
  |now − sig_timestamp| < 5 min
  nonce not in TTL cache
  → 200 / 401

NestJS JwtAuthGuard (локальная валидация JWT)
Controller → Service → engineAdapter.execute()
  → HTTP к engine c Authorization: Bearer <JWT>
  → Engine валидирует JWT ещё раз
```

Детали атак и threat matrix — `architecture-full-2026-04-22.md` раздел «Zero-Trust развёрнутый флоу».

### Публичные endpoints (`@SkipSignature`)

Доступны без ECDSA-подписи:

- `/auth/login`, `/auth/register`, `/auth/verify-signature`
- `/config` (maintenance mode)
- `/health*`
- `/notifications/telegram/webhook`
- `/files/upload`, `/files/:id/download` — **legacy** (security-пункт 16, план перевести на signed)

**WebSocket** подписи **нет** — только JWT на handshake (security-пункт 17).

## FastAPI bind

```env
API_HOST=127.0.0.1
API_PORT=8100
```

FastAPI слушает **только localhost** внутри rugpt-container. Единственный внешний путь — через nginx на `10.0.0.2:80` (WireGuard).

## Безопасность — краткое резюме

| Граница | Защита |
|---|---|
| Interne → Prod-VPS | TLS 1.2/1.3 (nginx + Let's Encrypt) |
| Prod-VPS ↔ Engine | WireGuard ChaCha20-Poly1305 |
| Engine LAN (PG/Kafka/Tika/LiteLLM) | plaintext, trust by LAN. **TLS отсутствует** (security-пункт 1) |
| App-layer запросы (mutation) | JWT + ECDSA signature + nonce cache |
| WebSocket | только JWT (без signature — известный gap) |
| File upload/download | `@SkipSignature` (legacy) |
| CORS Engine | `allow_origins=["*"]` — dev-настройка в prod (security-пункт 4) |

### Известные замечания

- **Telegram webhook** (`POST /api/v1/notifications/telegram/webhook`) **недоступен** — engine за VPN. Исходящий вызов `Engine → api.telegram.org` работает. Для входящего webhook нужен отдельный proxy через Prod-VPS (не реализовано).
- **Engine без rate-limit** — открыт в VPN без throttling (security-пункт 20). Rate-limit — только на webclient.
- **Nonce-cache** — in-memory, не persistent: при рестарте engine возникает replay-window (security-пункт 21).
- **Один JWT_SECRET** общий на webclient + engine — compromise одного = обоих (security-пункт 2).

Полный список — `tech-debt.md` раздел «Infrastructure & Security».
