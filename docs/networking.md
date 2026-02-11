# RuGPT Engine: Сетевая архитектура

## Обзор

Engine доступен для WebClient через VPN-сеть. Перед FastAPI стоит Nginx, который фильтрует входящие запросы.

```
                     VPN (WireGuard)
WebClient ──────────────────────────────────► Engine (10.0.0.2)
10.0.0.1                                         │
                                                  ▼
                                         ┌─── Nginx (:80) ───┐
                                         │                    │
                                         │  /api/v1/web/*     │
                                         │  → proxy :8100     │
                                         │  (strip /web)      │
                                         │                    │
                                         │  всё остальное     │
                                         │  → 403 Forbidden   │
                                         └────────────────────┘
                                                  │
                                                  ▼
                                         ┌─── FastAPI (:8100) ┐
                                         │  /api/v1/auth/*    │
                                         │  /api/v1/users/*   │
                                         │  /api/v1/roles/*   │
                                         │  /api/v1/chats/*   │
                                         │  /api/v1/health    │
                                         └────────────────────┘
```

## VPN-сеть

| Хост | IP | Роль |
|------|----|------|
| WebClient (VPS) | 10.0.0.1 | Тонкий proxy, отправляет запросы к Engine |
| Engine (офис) | 10.0.0.2 | Бэкенд, хранит все данные, обрабатывает LLM |

VPN создаётся между машинами для изоляции трафика. Engine не открыт в интернет.

## Nginx конфигурация

Nginx на машине Engine (10.0.0.2) выполняет роль reverse proxy и фильтра:

```nginx
server {
    listen 80;
    server_name 10.0.0.2;

    # Только запросы с prefix /api/v1/web/ принимаются
    location /api/v1/web/ {
        # Убираем /web из пути при проксировании
        rewrite ^/api/v1/web/(.*)$ /api/v1/$1 break;
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Таймаут для LLM запросов (долгая генерация)
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }

    # Всё остальное — отбросить
    location / {
        return 403;
    }
}
```

### Что делает Nginx

1. **Фильтрация по prefix `/web`**: Только запросы вида `/api/v1/web/*` проходят. Это гарантия, что обращение пришло от WebClient через VPN, а не случайный запрос.
2. **Rewrite**: Убирает `/web` из пути. Запрос `/api/v1/web/auth/login` → проксируется на `http://127.0.0.1:8100/api/v1/auth/login`
3. **Reject**: Всё что не начинается с `/api/v1/web/` получает 403 Forbidden.
4. **Timeout**: Увеличенный `proxy_read_timeout` для запросов к LLM (генерация может занять до 5 минут на CPU).

## Маппинг роутов

WebClient отправляет на `/api/v1/web/*`, Nginx перенаправляет на `/api/v1/*`:

| WebClient запрос | Nginx rewrite | FastAPI обработка |
|------------------|---------------|-------------------|
| `POST /api/v1/web/auth/login` | `/api/v1/auth/login` | `auth.py` |
| `GET /api/v1/web/users` | `/api/v1/users` | `users.py` |
| `GET /api/v1/web/roles` | `/api/v1/roles` | `roles.py` |
| `GET /api/v1/web/chats/my` | `/api/v1/chats/my` | `chats.py` |
| `POST /api/v1/web/chats/{id}/messages` | `/api/v1/chats/{id}/messages` | `chats.py` |
| `GET /api/v1/web/health` | `/api/v1/health` | `health.py` |

## FastAPI

FastAPI слушает на `127.0.0.1:8100` (только localhost). Внешний доступ только через Nginx.

```env
API_HOST=127.0.0.1
API_PORT=8100
```

## Безопасность

- Engine **НЕ открыт в интернет** — только VPN
- Nginx **отсекает** всё кроме `/api/v1/web/*`
- FastAPI слушает **только localhost** (`127.0.0.1`)
- VPN трафик шифрован (WireGuard)
