# Технический долг

## Deploy: rugpt/deploy.sh

| # | Проблема | Приоритет | Статус | Описание |
|---|----------|-----------|--------|----------|
| 1 | ~~Нет `set -e`~~ | ~~Критичный~~ | Исправлено | `set -e` добавлен |
| 2 | ~~Нет рестарта uvicorn~~ | ~~Критичный~~ | Исправлено | `local_restart.sh` вызывается из `deploy.sh` |
| 3 | ~~Нет автоматических миграций~~ | ~~Критичный~~ | Исправлено | `local_restart.sh` запускает миграции перед стартом |
| 4 | Нет `--delete` в rsync | Средний | Открыто | Удалённые локально файлы остаются на проде. Добавить `--delete` с exclude .env и prod-only файлов. |
| 5 | ~~Нет проверки доступности сервера~~ | ~~Низкий~~ | Неактуально | С `set -e` ssh-ошибка остановит скрипт |
| 6 | Нет бэкапа/отката | Низкий | Открыто | Нет механизма отката к предыдущей версии |

## Deploy: webclient_rugpt/deploy.sh

| # | Проблема | Приоритет | Статус | Описание |
|---|----------|-----------|--------|----------|
| 1 | `FRONTEND_URL` = `localhost:3001`, порт маппится на 3000 | Критичный | Открыто | `wait_for_frontend` всегда fail. Maintenance может остаться навечно. |
| 2 | Trap не вызывает `disable_maintenance` | Критичный | Открыто | При падении скрипта trap вызывает только `cleanup_docker`, maintenance остаётся. |
| 3 | `sleep 30` при каждом деплое | Средний | Открыто | Maintenance уже включён, можно сократить до 3-5 секунд. |
| 4 | Restore не проверяет frontend | Низкий | Открыто | `wait_for_frontend` не вызывается после restore. |

## Engine: код

| # | Проблема | Приоритет | Описание |
|---|----------|-----------|----------|
| 1 | `/health/ready` не проверяет БД | Средний | TODO в коде: `# TODO: Check database connectivity`. Всегда возвращает `ready: True`. Deploy health checks могут пройти при недоступной БД. |
| 2 | `web_search` и `role_call` -- stubs | Средний | Возвращают placeholder строки. Агент может пытаться вызвать несуществующий функционал. |
| 3 | Bare `except Exception` в scheduler | Низкий | ~13 блоков в `scheduler_service.py` которые только логируют ошибку. Нет alerting или circuit-breaking при массовых сбоях. |

## Support: in-app уведомление оператору на сообщение в тикете

| # | Проблема | Приоритет | Статус |
|---|----------|-----------|--------|
| 1 | Оператор не получает in-app уведомление на новое сообщение внутри уже взятого тикета | Средний | Открыто (запланировано) |

**Контекст.** Оператор получает in-app уведомление (и рост красного бейджа в сайдбаре через WS `notification:new`) только на:
- новый тикет в очереди / эскалацию how_to → `SupportNotificationService.notify_new_in_queue` (fan-out всем операторам);
- переоткрытие → `notify_reopened` (assignee либо очередь);
- взятие/закрытие → пинги соответствующей стороне.

А **ответное сообщение requester'а внутри уже открытого тикета** in-app уведомления оператору НЕ создаёт. Такое сообщение идёт обычным чат-путём (`POST /chats/{id}/messages` → `chat_service` → Kafka `chat.events` `kind:message` → `broadcastToChat`) — долетает в комнату чата по WS, но support-бейдж/колокольчик у оператора от него не растёт. Пока оператор не открыл сам чат, он не видит, что клиент дописал.

**Что сделать (engine).** В пути отправки сообщения для SUPPORT-чата создавать in-app уведомление назначенному оператору, когда пишет requester:
- Точка: `routes/chats.py:send_message` (после сохранения user-сообщения) либо отдельный хук в `support_ticket_service` по аналогии с `handle_incoming_message`. Тикет резолвится по `chat.support_ticket_id`.
- Условие: `chat.type == SUPPORT` И у тикета есть `assignee_user_id` И отправитель == `requester_user_id` (не оператор, не AI). Уведомление шлём на `assignee_user_id`.
- Создание: `in_app_notification_service.create(user_id=assignee, org_id=RUGPT_SUPPORT_ORG_ID, type='system', title='Новое сообщение в тикете', content=<preview>, reference_type='support_ticket', reference_id=ticket.id)`. WS-пуш `notification:new` и рост бейджа у оператора получатся **автоматически** — `InAppNotificationService.create` уже публикует в `chat.events` (realtime-механизм уже на месте).

**Анти-дубль / тонкости.**
- Не уведомлять на собственные сообщения отправителя и на AI-сообщения (`sender_type == ai_role`).
- Тикет без assignee (ещё в очереди / how_to в AI-first-line) — не трогаем: его покрывают `notify_new_in_queue` / реопен-fan-out. Очередь на сообщение в неназначенный тикет уведомлять не нужно.
- Не путать с чат-unread: support in-app — это про бейдж/колокольчик (`useSupportUnread` считает `reference_type='support_ticket'`), он независим от чат-unread.
- **Антиспам:** серия сообщений клиента подряд = N уведомлений → бейдж раздувается. Рассмотреть дедуп «не более одного непрочитанного support-уведомления на тикет» (перед созданием проверять, нет ли уже непрочитанного с тем же `reference_id`).

**Парная фронт-доработка (mark-read).** По аналогии с очередью: открытие оператором чата тикета (`/chat/support/{id}`) должно помечать прочитанным in-app уведомление этого тикета (PATCH read по уведомлениям с `reference_id == ticketId`) и слать `window` event `support-notifications-read`, чтобы бейдж падал. Сейчас mark-read реализован только на заходе в очередь (`/support/queue`).

**Затрагивает:** engine (`routes/chats.py` или `support_ticket_service` + `in_app_notification_service`) → нужен redeploy движка; фронт (`chat/support/[id]/page.tsx` — mark-read на открытии чата).

## Infrastructure & Security

> Источник — security-аудит из `architecture-full-2026-04-22.md` (раздел «Security-аудит — точки внимания»), 25 пунктов. Сгруппировано по темам.

### Крипто и секреты

| # | Проблема | Приоритет | Описание |
|---|----------|-----------|----------|
| 1 | TLS отсутствует в LAN | Высокий | PG, Kafka, Tika, LiteLLM, Redis — plaintext между узлами. Сниффер в `192.168.1.0/24` видит SQL-query, embeddings, промпты, пароли в bcrypt-hash. |
| 2 | Один JWT_SECRET на webclient + engine | Высокий | Compromise одного = compromise обоих. Нужно разделить: webclient только валидирует локально, engine подписывает. |
| 3 | Plaintext .env | Высокий | Секреты в `.env` plaintext на engine, webclient, GitLab-runner, LiteLLM. Нет vault (HashiCorp/SOPS/age), нет rotation. |
| 4 | CORS Engine = `*` | Высокий | `allow_origins=["*"]` в `app.py` — dev-настройка в prod. Engine за VPN, но всё равно ослабляет Zero-Trust (CSRF через скомпрометированный webclient). |
| 5 | LiteLLM auth = `sk-dummy` | Средний | Любой непустой Bearer-токен принимается. В LAN = открытый LLM для любого процесса на B/C. |
| 6 | Kafka без SASL/TLS | Средний | `docker-vm :9092` доверяет по VPN. Любой процесс в VPN/LAN может читать `chat.events` (приватные сообщения) и публиковать в `agent.requests`. План SASL_PLAINTEXT после Alpha. |
| 7 | Redis без password в dev | Низкий | `REDIS_PASSWORD` не задан в dev — проверить что в prod задан. AOF в `redis:7-alpine` хранит историю в файле. |
| 8 | MAINTENANCE_USERS в `.docker.env` | Средний | Список email:password bcrypt-проверяется на webclient локально, минуя engine. Утечка `.docker.env` → байпас всей auth во время maintenance. |

### App-layer / Zero-Trust

| # | Проблема | Приоритет | Описание |
|---|----------|-----------|----------|
| 9 | File upload/download без signature | Высокий | `/files/upload` и `/files/:id/download` помечены `@SkipSignature` (legacy). Украденный JWT = полный доступ к файлам org. |
| 10 | WebSocket без подписи | Высокий | После JWT-handshake события доверяются. Украденный JWT → можно открыть WS и слушать/отправлять в комнатах `chat:<id>`. |
| 11 | Нет admin revoke device endpoint | Средний | При компрометации устройства нет API убрать его `public_key` из `user_devices`. Только прямой SQL. |
| 12 | Nonce-cache не persistent | Средний | In-memory TTL-cache в engine-процессе. Рестарт → replay-window ±5 min. Нужен Redis-backed или БД-backed cache. |
| 13 | Engine compromise = TOTAL LOSS | Высокий | Engine видит `user_devices.public_key` + `JWT_SECRET` → может генерить токены и валидировать любые подписи. Нет HSM / split-trust. |
| 14 | Engine без rate-limit | Средний | Открыт в VPN без throttling. Скомпрометированный webclient может DoS'ить engine или brute-force логины. |

### Инфраструктура

| # | Проблема | Приоритет | Описание |
|---|----------|-----------|----------|
| 15 | docker-vm multi-tenant | Высокий | Prod Kafka + Tika + n8n + сторонние проекты владельца на одной Docker-VM. CVE/misconfig в side-контейнере → pivot к prod-данным. Решение: rootless Docker / gVisor / отдельная VM. |
| 16 | docker.sock в gitlab-runner | Высокий | `/var/run/docker.sock` смонтирован в runner-vm (Docker executor). Злонамеренный CI-job = побег на хост. Рекомендация — kaniko/buildkit. |
| 17 | Kernel sharing на Proxmox LXC | Средний | rugpt-container + gitlab-container делят ядро Proxmox-хоста. Kernel-CVE → эскалация на все LXC + host. Smягчить: KVM вместо LXC для критичных сервисов. |
| 18 | Публичные SSH через NAT | Средний | `:28351` → GitLab SSH, `:24952` → runner SSH. Brute-force target. Нужен fail2ban + key-only auth + port knocking. |
| 19 | Dual-WAN failover ACL | Средний | При переключении на ISP #2 ACL могут не подняться. Нужен тест failover-сценария. |
| 20 | Apache Tika CVE-prone | Средний | Java + парсеры PDF/DOCX — регулярные CVE. Запускать в отдельном контейнере, без сети наружу, periodically update. |
| 21 | NAS single point of failure | Средний | Synology NAS + RAID 5 + WriteOnce спасает от bit-rot и delete, но не от пожара/кражи здания. Нужен offsite (S3/backblaze/второй NAS в другой локации). |
| 22 | Dev-среда с клоном prod-данных | Высокий | `dev-vm` (B.II.4) планирует клонировать prod-PG. Нужна scripted анонимизация (PII-hash, email rewrite, strip tokens) перед импортом. |
| 33 | PostgreSQL на дефолтных настройках | Высокий | postgres-vm имеет **368GB RAM / 35 cores / 200+2000GB**, но `postgresql.conf` стоит как после `apt install`: `max_connections=100`, `shared_buffers=128MB` (≈ 0.03% памяти), `work_mem`/`effective_cache_size` дефолтные. PG использует <1% доступных ресурсов. Любой соседний клиент (например pgbench от @S0avy — см. инцидент 11.05.2026) забивает 100 non-reserved слотов → engine не может взять коннект → каскадный 503 «Signature verifier unavailable» на webclient → пустой UI у всех юзеров. **Минимальный тюнинг:** `max_connections=500`, `superuser_reserved_connections=10`, `shared_buffers=92GB`, `effective_cache_size=276GB`, `work_mem=32MB`, `maintenance_work_mem=2GB`, `wal_buffers=64MB`. Требует рестарт PG (maintenance-окно). Также `ALTER USER rugpt CONNECTION LIMIT 50` — per-user лимит чтобы один сервис не сожрал всё. |
| 34 | Нет connection pooler перед PostgreSQL | Средний | Engine использует app-level asyncpg pool (size 2-10). Любой новый сервис / параллельный воркер транслируется в physical connections 1-к-1. Это и привело к инциденту pgbench: 90 параллельных подключений → исчерпание `max_connections`. **Решение:** `pgbouncer` в transaction-pooling режиме перед PG. App pool настраивается как угодно, реальных PG-коннектов ~20-30 даже при тысячах client-side. Закрывает 33 и сам по себе делает 33 менее срочным. Запустить на postgres-vm как systemd-сервис, engine/webclient_rugpt/любой клиент переключить через DSN `postgres://...:6432/rugpt` (порт pgbouncer'а). Подсказка: `pool_mode=transaction`, `max_client_conn=2000`, `default_pool_size=20`. |
| 35 | Нет алертов на исчерпание PG-коннектов | Средний | Инцидент 11.05.2026 (pgbench исчерпал слоты) узнали только когда юзеры пожаловались на пустой UI. Нужен Prometheus exporter (`postgres_exporter`) + alert `pg_stat_activity_count / max_connections > 0.8` → Telegram админу. Аналогично — alert на `idle in transaction > 5min`. Заодно покрывает другие инциденты с пулом. |

### CI/CD + Observability + DR

| # | Проблема | Приоритет | Описание |
|---|----------|-----------|----------|
| 23 | GitHub → GitLab миграция | Высокий | План: отозвать GitHub PAT/SSH-ключи сразу после миграции. Оставить read-only mirror или удалить полностью. |
| 24 | Нет signed commits/artifacts | Средний | GitLab CI должен подписывать теги + deploy-артефакты (sigstore/cosign). Сейчас любой с CI-vars может подменить binary. |
| 25 | SSH-keys на Macbook — SPOF | Высокий | Компрометация macbook = root SSH на Prod-VPS + rugpt-container + Dev-VPS. HW-key (YubiKey) рекомендуется. |
| 26 | Нет Sentry / Prometheus / Loki / ELK | Средний | Engine — только файловые логи в `/root/rugpt/logs/`. Webclient — `docker logs` (план nestjs-pino daily-dir). PG/Kafka — `docker logs`. Алертов нет. |
| 27 | Нет метрик по Kafka | Средний | Consumer lag, producer errors, partition skew не мониторятся. При padding `agent.requests` никто не узнает. |
| 28 | Telegram webhook недоступен | Низкий | Engine за VPN, `POST /notifications/telegram/webhook` из интернета не проходит. Исходящие вызовы engine → Telegram работают. Для входящего нужен отдельный public proxy. |
| 29 | Apache Kafka KRaft single-node | Средний | Одна нода без репликации — при падении `docker-vm` всё встаёт. После Alpha — 3-нодовый кластер либо managed. |
| 30 | Миграции без rollback-стратегии | Низкий | 18 SQL миграций forward-only. При ошибке в prod-деплое нет автоматического rollback (только ручной restore из pg_dump). |
| 31 | `KAFKA_ENABLED=false` sync fallback в AIService | Средний | `Config.KAFKA_ENABLED=false` переключает `try_auto_respond`, `process_ai_mentions` и др. в синхронный режим (HTTP блокируется до ответа LLM). Введено для тестов и graceful degradation, но: (1) каждая агентная фича требует двух реализаций; (2) sync режим — костыль для проактивных scheduler-вызовов; (3) при падении Kafka chat.events тоже мёртв, юзер всё равно не получит ответ через WS. **План:** Kafka как hard dependency (как PostgreSQL), local dev через `docker-compose.kafka.yml`, тесты через testcontainers/моки. Опросная фича (`docs/superpowers/specs/2026-04-30-poll-ai-dialog-design.md`) написана Kafka-only — не плодит долг. |
| 32 | Identity на ~114 endpoint'ах берётся из JWT, а не из signed payload | Средний | Большинство engine-роутов используют `Depends(get_current_user)` (парсинг JWT) для идентификации актора. Подписанный `user_id` (из Zero Trust signature, проверенный SignatureGuard'ом) при этом игнорируется. Если JWT украден без устройства — атакующий ходит куда угодно. На уровне chat-рутов `validate_message`/`reject_message` уже мигрированы на signed `user_id` (май 2026), но `users.py`, `tasks.py`, `projects.py`, `task_polls.py`, `notifications.py`, `in_app_notifications.py`, `calendar.py`, `corrections.py`, `rag.py`, `support.py`, `files.py`, `departments.py`, `roles.py`, `organizations.py` — нет. **Прагматичный fast-fix перед массовой миграцией:** в `get_current_user` (или в SignatureGuard на webclient) сравнивать signed user_id с JWT user_id и валиться 401 если разные — закрывает кражу JWT-без-устройства без переписывания каждого endpoint'а. **Полный фикс:** заменить везде на `user_id: UUID` query-параметр + storage lookup для is_admin/org_id. Эпик на сутки работы, риск регресса на любом из 100+ роутов. |

## Файловые папки — отложенные улучшения (2026-05-13)

Базовая фича папок выкачена без интеграции с агентами. Идеи:

| ID | Идея | Зачем | Оценка |
|---|---|---|---|
| TD-FOLDERS-1 | folder path в `list_documents` (engine tool) | LLM видит `/Договоры/2024/НДА.pdf`. Лучший контекст | ~30 строк: batch path-resolve + format-строка |
| TD-FOLDERS-2 | Фильтр `list_documents` по `folder_id` | LLM сужает поиск «в папке X» | ~50 строк: optional param + `folder_storage.list_subtree_ids` |
| TD-FOLDERS-3 | Новый tool `get_directory_tree` | LLM получает ASCII-карту дерева юзера | ~50 строк: новый `tools/get_directory_tree.py` + ToolRegistry |
| TD-FOLDERS-4 | `rag_search` scoped to folder | Семантический поиск ограничен subtree. Риск регрессии RAG | ~80 строк pre-filter в Python, ~150 при SQL-уровне |
| TD-FOLDERS-7 | Восстановление soft-deleted папок и файлов | «Trash bin» с undo | Reactivate service-методы + UI `/trash` |
| TD-FOLDERS-8 | Periodic cleanup orphan storage bytes | Cron-job удаляет физические файлы `is_active=false older than 30d` | Scheduler-task |
| TD-FOLDERS-9 | Public/shared org folders | Папки видимые всем org. Требует отдельного дизайна | Полноценная фича — новый brainstorming |
| TD-FOLDERS-10 | Folder reordering | Кастомный порядок (`order_index`) | Migration + drag handle |
| TD-FOLDERS-11 | Admin-доступ к чужому дереву папок | Сейчас `useFolders` всегда фетчит дерево текущего юзера. Engine API уже поддерживает `?user_id=` для admin'а, нужно протащить через webclient + UI selector | Frontend: расширить хук, добавить переключатель в UI; backend уже готов |
| TD-FOLDERS-12 | Восстановить отсутствующие миграции на dev | dev DB после апгрейда PG 14→16 не содержит таблиц `correction_rules`, `chunks`, `tables_rows_chunks` — миграции 010, 012, 013 не применены. Engine на dev падает на RAG/correction-кодпасах. Нужно либо вручную применить эти миграции, либо restore из prod-дампа | Низкий приоритет — не блокирует dev-разработку фич, но ограничивает что можно тестировать локально |

Spec: `docs/superpowers/specs/2026-05-12-file-folders-design.md`. Plan: `docs/superpowers/plans/2026-05-12-file-folders.md`.
