# PM-агент, Kafka event bus и асинхронный инференс — дизайн

> Дата: 2026-04-14
> Статус: draft к утверждению
> Пункт роадмапа: 10 (PM-агент) + часть 5 (очереди к нейронкам) + разблокировка 12

## Контекст

П.10 требует, чтобы PM-агент проактивно писал сообщения в личные чаты юзеров при
изменениях задач. Но у текущей архитектуры нет канала доставки сообщений от
Engine к подключённым WS-клиентам: WebSocket живёт только в NestJS,
`io.emit(...)` вызывается синхронно в HTTP-pipeline, и любое сообщение, которое
Engine сохраняет напрямую через `message_storage.create`, невидимо до
refresh-а страницы.

Параллельно в роадмапе есть п.5 — очереди к нейронкам. Сейчас LLM-вызовы
блокирующие: HTTP-запрос юзера держит соединение открытым 30-300 секунд, пока
LangGraph крутит ReAct loop. При рестарте Engine запрос теряется, retry нет,
backpressure нет.

Оба пункта решаются одним механизмом — **Kafka event bus между Engine и
NestJS**, плюс асинхронный паттерн «HTTP принял → 202 → фоновый consumer
выполнил → результат через шину → WS broadcast». Этот документ описывает
фундамент, который закрывает п.10 целиком и делает основу для п.12.

## Цели

1. **PM-агент работает**: seed `pm` роли и system user, `TaskNotificationService`
   пишет сообщения в личные чаты при переходах задач, пользователи видят их
   live через WebSocket.
2. **Kafka event bus** между Engine и NestJS: Engine publish'ит сообщения,
   NestJS consume'ит и broadcast'ит через Socket.IO.
3. **Асинхронный LLM inference**: все агентные вызовы идут через очередь
   `agent.requests`, Engine возвращает HTTP 202 мгновенно, ответ прилетает
   позже через `chat.events`.
4. **Idempotency** — повторная обработка запроса из Kafka не создаёт дубликатов
   AI-сообщений и не запускает тот же агентный run дважды.
5. **Backward-compatible для фронта** где это возможно: фронт уже умеет
   отображать AI-сообщения через WS event `message`, контракт остаётся.

## Объём

### Входит

- Инфраструктура: Kafka (KRaft mode, без ZooKeeper) на Engine-машине
- Engine: `kafka/producer.py`, `kafka/consumer.py`, `kafka/bus_service.py`,
  background consumer loop через `asyncio.create_task` в `startup_event`
- Миграция `018_pm_role_and_agent_runs.sql`:
  - PM роль + system user `pm` в системной org
  - Таблица `agent_runs(request_id PK, status, ...)` для idempotency
- Файл `src/engine/prompts/pm.md` — system prompt для PM
- Новый сервис `TaskNotificationService`, интеграция в `TaskService`
- Рефакторинг `AIService`: блокирующий `agent_executor.run` → publish в
  `agent.requests`, возврат HTTP 202
- Background consumer `agent.requests`: подхватывает запрос, запускает
  `agent_executor.run(...)` локально, сохраняет AI message, публикует
  `chat.events`
- `chat.events` publisher вызывается из двух мест: `TaskNotificationService`
  после сохранения PM-сообщения, и из consumer'а `agent.requests` после
  сохранения AI-сообщения
- NestJS: `KafkaConsumerService` с подпиской на `chat.events`, broadcast через
  `SocketGateway.io.to('chat:<id>').emit(...)`
- Engine adapter / NestJS `ChatService`: ответ `POST /messages` меняется на
  `{user_message, agent_pending: boolean}` (без `ai_responses`)
- Фронт: убирает ожидание `ai_responses` в HTTP-ответе, полагается на
  существующий WS event `message`. Опционально — typing indicator при
  `agent_pending=true`
- Тесты engine pytest: `test_task_notifications.py`, `test_kafka_bus.py`
  (in-memory fake producer/consumer), `test_agent_runs_idempotency.py`
- `local_restart.sh` обновление — не добавляет новых процессов, Kafka
  управляется системно (systemd или docker-compose)

### НЕ входит

- Интерактивные кнопки в сообщениях PM (явно отброшено в спеке п.10)
- `acting_on_behalf_of_user_id` (отброшено)
- Conversational tools для PM (`chat_post`, `summary_*`) — это п.12, но
  архитектура event-шины уже готова их принять
- Отдельные worker-процессы для инференса (consumer живёт в том же Engine
  процессе; разделение через `gunicorn -w N` или отдельный entrypoint —
  опциональная миграция позже)
- Dead letter queue, monitoring dashboard — базовые, без продвинутого
  наблюдения
- Schema registry для Kafka payload'ов (JSON, контракт в коде)
- Cross-org чаты системных юзеров с обычными — уже работают, не трогаем

## Архитектура

### Общий поток сообщений

```
┌──────────┐     ┌─────────┐     ┌───────────┐     ┌──────────┐     ┌─────────┐
│ Browser  │─HTTP│ NestJS  │─HTTP│  Engine   │─pub─│  Kafka   │─sub─│ NestJS  │
│ (React)  │     │ (proxy) │     │ (FastAPI) │     │ (broker) │     │ consumer│
└──────────┘     └────┬────┘     └─────┬─────┘     └──────────┘     └────┬────┘
                      │                │                                 │
                      │                │                                 │
                      ▼                ▼                                 ▼
                  io.emit()       inline consumer                    io.emit()
                  user message    (async agent run)                  AI/PM message
                  через WS        → publish                          через WS
                                  chat.events
```

Два Kafka топика:

| Топик | Producer | Consumer | Партиции | Purpose |
|---|---|---|---|---|
| `agent.requests` | Engine HTTP handler | Engine background task | 3 | Задания на инференс, durability, retry |
| `chat.events` | Engine (из `TaskNotificationService` + `agent.requests` consumer) | NestJS `SocketGateway` | 3 | Готовые сообщения для доставки в WS |

**Ключевой инвариант:** Kafka `agent.requests` — это внутренний канал Engine,
NestJS о нём не знает. `chat.events` — внешний канал для доставки, NestJS в
него только читает.

### Kafka infrastructure

**Broker:** Kafka 3.5+ в KRaft mode (single-node для Alpha, без ZooKeeper).

**Размещение:** на Engine-машине (5090/Proxmox). NestJS ходит через WireGuard
VPN к `10.0.0.2:9092`. Persistent consumer connection, VPN-трафик
активизируется только при новых сообщениях.

**Установка:** Docker Compose unit в `/root/rugpt/docker-compose.kafka.yml`:

```yaml
services:
  kafka:
    image: bitnami/kafka:3.5
    ports:
      - "9092:9092"
    environment:
      - KAFKA_CFG_NODE_ID=1
      - KAFKA_CFG_PROCESS_ROLES=controller,broker
      - KAFKA_CFG_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093
      - KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://10.0.0.2:9092
      - KAFKA_CFG_CONTROLLER_LISTENER_NAMES=CONTROLLER
      - KAFKA_CFG_CONTROLLER_QUORUM_VOTERS=1@localhost:9093
      - KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
    volumes:
      - kafka_data:/bitnami/kafka

volumes:
  kafka_data:
```

Topic provisioning — через админ-скрипт `scripts/kafka_init.sh` (идемпотентный):

```bash
kafka-topics.sh --create --if-not-exists --topic agent.requests \
  --partitions 3 --replication-factor 1 --bootstrap-server localhost:9092
kafka-topics.sh --create --if-not-exists --topic chat.events \
  --partitions 3 --replication-factor 1 --bootstrap-server localhost:9092
```

Авторизация: на Alpha без SASL/SSL, доверие по VPN. После Alpha — добавить
`SASL_PLAINTEXT` с `PLAIN` mechanism.

**Retention:**
- `agent.requests`: 24 часа (запрос долго не должен висеть)
- `chat.events`: 1 час (делivery почти мгновенная, retention только для retry
  при рестарте NestJS)

### Engine: Kafka producer

Новый файл `src/engine/kafka/producer.py`:

```python
import asyncio
import json
import logging
from typing import Optional
from aiokafka import AIOKafkaProducer

from ..config import Config

logger = logging.getLogger("rugpt.kafka.producer")


class KafkaProducerService:
    """Thin async wrapper over aiokafka producer. Single instance owned by EngineService."""

    def __init__(self, bootstrap_servers: str = None):
        self.bootstrap_servers = bootstrap_servers or Config.KAFKA_BOOTSTRAP_SERVERS
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self):
        if self._producer is not None:
            return
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if isinstance(k, str) else k,
            enable_idempotence=True,  # exactly-once producer semantics
            acks="all",
        )
        await self._producer.start()
        logger.info(f"Kafka producer started, brokers={self.bootstrap_servers}")

    async def stop(self):
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            logger.info("Kafka producer stopped")

    async def send(self, topic: str, value: dict, key: Optional[str] = None):
        if self._producer is None:
            raise RuntimeError("KafkaProducerService not started")
        # Partition key: chat_id (for ordering per chat) or agent request_id
        await self._producer.send_and_wait(topic, value=value, key=key)
```

### Engine: Kafka consumer (background task)

Новый файл `src/engine/kafka/consumer.py`:

```python
import asyncio
import json
import logging
from typing import Callable, Awaitable
from aiokafka import AIOKafkaConsumer

from ..config import Config

logger = logging.getLogger("rugpt.kafka.consumer")

MessageHandler = Callable[[dict], Awaitable[None]]


class KafkaConsumerLoop:
    """Background consumer for a single topic. Owned by EngineService."""

    def __init__(
        self,
        topic: str,
        group_id: str,
        handler: MessageHandler,
        bootstrap_servers: str = None,
    ):
        self.topic = topic
        self.group_id = group_id
        self.handler = handler
        self.bootstrap_servers = bootstrap_servers or Config.KAFKA_BOOTSTRAP_SERVERS
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def start(self):
        self._task = asyncio.create_task(self._run(), name=f"kafka-{self.topic}")

    async def stop(self):
        self._stopping = True
        if self._task:
            await asyncio.wait([self._task], timeout=5.0)

    async def _run(self):
        consumer = AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            enable_auto_commit=False,  # manual commit after successful handle
            auto_offset_reset="earliest",
        )
        await consumer.start()
        logger.info(f"Kafka consumer started: topic={self.topic} group={self.group_id}")
        try:
            while not self._stopping:
                batch = await consumer.getmany(timeout_ms=1000, max_records=10)
                for tp, messages in batch.items():
                    for msg in messages:
                        try:
                            await self.handler(msg.value)
                            await consumer.commit({tp: msg.offset + 1})
                        except Exception as e:
                            logger.error(
                                f"Handler failed for {self.topic} offset={msg.offset}: {e}",
                                exc_info=True,
                            )
                            # don't commit: message will be retried
        finally:
            await consumer.stop()
            logger.info(f"Kafka consumer stopped: {self.topic}")
```

Запуск в `EngineService.initialize()`:

```python
await self.kafka_producer.start()

self.agent_request_consumer = KafkaConsumerLoop(
    topic="agent.requests",
    group_id="engine-agent-runners",
    handler=self._handle_agent_request,
)
await self.agent_request_consumer.start()
```

### Миграция 018

`src/engine/migrations/018_pm_role_and_agent_runs.sql`:

```sql
-- Migration 018: PM agent role + system user + agent_runs idempotency table

-- ============================================
-- 1. PM role in RuGPT system org
-- ============================================
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    pm_role_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Проджект-менеджер',
        'pm',
        'AI-ассистент для управления задачами и уведомлений',
        'Вы — PM-агент, проджект-менеджер.',
        'qwen3:14b',
        'simple',
        '["task_create", "task_query", "task_update"]'::jsonb,
        'pm.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO pm_role_id;

    IF pm_role_id IS NULL THEN
        SELECT id INTO pm_role_id FROM roles WHERE org_id = rugpt_org_id AND code = 'pm';
    END IF;

    -- System user 'pm'
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'PM-агент', 'pm', 'pm@rugpt.system', NULL,
        pm_role_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;
END $$;

-- ============================================
-- 2. agent_runs table (idempotency for async agent execution)
-- ============================================
CREATE TABLE IF NOT EXISTS agent_runs (
    request_id UUID PRIMARY KEY,
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    triggering_user_id UUID REFERENCES users(id),
    role_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs(chat_id, created_at DESC);

COMMENT ON TABLE agent_runs IS 'Tracks async agent executions. Used for idempotency (Kafka retry) and audit.';
COMMENT ON COLUMN agent_runs.status IS 'pending | running | done | failed';
```

### Engine: `AgentRunStorage` + idempotency

Новый `src/engine/storage/agent_run_storage.py`:

```python
class AgentRunStorage(BaseStorage):
    async def create(self, run: AgentRun) -> AgentRun: ...
    async def get(self, request_id: UUID) -> Optional[AgentRun]: ...
    async def mark_running(self, request_id: UUID) -> bool: ...  # returns True if transition succeeded
    async def mark_done(self, request_id: UUID, result_message_id: UUID) -> None: ...
    async def mark_failed(self, request_id: UUID, error: str) -> None: ...
```

**Idempotency логика в consumer'е `agent.requests`:**

```python
async def _handle_agent_request(self, payload: dict):
    request_id = UUID(payload["request_id"])
    # атомарный CAS: pending -> running. Если уже running/done — скип.
    acquired = await self.agent_run_storage.mark_running(request_id)
    if not acquired:
        existing = await self.agent_run_storage.get(request_id)
        logger.info(f"agent_run {request_id} already in status={existing.status}, skip")
        return

    try:
        result = await self.agent_executor.run(
            role_code=payload["role_code"],
            prompt_context=payload["prompt_context"],
            ...
        )
        ai_msg = await self.chat_service.send_ai_message(
            chat_id=UUID(payload["chat_id"]),
            content=result.content,
            reply_to_id=UUID(payload["user_message_id"]),
        )
        await self.agent_run_storage.mark_done(request_id, ai_msg.id)
        # publish в chat.events
        await self.kafka_producer.send(
            "chat.events",
            {"chat_id": payload["chat_id"], "message": ai_msg.to_dict()},
            key=payload["chat_id"],
        )
    except Exception as e:
        await self.agent_run_storage.mark_failed(request_id, str(e))
        raise  # consumer не коммитит, Kafka retry
```

`mark_running` — атомарный SQL:

```sql
UPDATE agent_runs SET status='running', started_at=NOW()
WHERE request_id=$1 AND status='pending'
RETURNING request_id
```

Если вернуло строку — значит этот инстанс выиграл гонку. Если нет — значит
другой consumer уже забрал либо run уже done.

### Engine: рефакторинг `AIService`

Было:
```python
async def process_ai_mentions(self, message, org_id):
    for mention in ai_mentions:
        ai_message = await self._run_agent(...)
        ai_messages.append(ai_message)
    return ai_messages
```

Стало:
```python
async def process_ai_mentions(self, message, org_id):
    for mention in ai_mentions:
        request_id = uuid4()
        run = AgentRun(
            request_id=request_id,
            chat_id=message.chat_id,
            user_message_id=message.id,
            triggering_user_id=message.sender_id,
            role_code=mention.role_code,
            status="pending",
        )
        await self.agent_run_storage.create(run)
        await self.kafka_producer.send(
            "agent.requests",
            {
                "request_id": str(request_id),
                "chat_id": str(message.chat_id),
                "user_message_id": str(message.id),
                "triggering_user_id": str(message.sender_id),
                "role_code": mention.role_code,
                "prompt_context": self._build_prompt_context(...),
            },
            key=str(message.chat_id),
        )
    return []  # no synchronous responses
```

HTTP response у `POST /chats/{id}/messages` меняется:

```python
return SendMessageResponse(
    user_message=MessageResponse(**user_msg.to_dict()),
    ai_responses=[],       # всегда пусто при async flow
    agent_pending=has_ai_mentions,  # новое поле
)
```

### PM notifications

`TaskNotificationService` реализуется как в спеке п.10, с одним отличием:
после `message_storage.create(msg)` дополнительно делает `kafka_producer.send`
в `chat.events`:

```python
async def _post(self, recipient_user_id, recipient_org_id, text, task_id):
    pm_id = await self._get_pm_user_id()
    if pm_id is None: return

    chat = await self.chat_service.create_direct_chat(pm_id, recipient_user_id, recipient_org_id)
    msg = Message(
        chat_id=chat.id,
        sender_id=pm_id,
        sender_type=SenderType.AI_ROLE,
        content=text,
        ai_is_valid=True,  # auto-validated, not a response to be reviewed
        created_at=datetime.utcnow(),
    )
    created = await self.message_storage.create(msg)
    # Publish to chat.events for WS delivery
    await self.kafka_producer.send(
        "chat.events",
        {"chat_id": str(chat.id), "message": created.to_dict()},
        key=str(chat.id),
    )
```

**PM lookup**: `user_storage.get_system_user_by_username('pm')` — метод
существует, используется напрямую. Спек п.10 содержал опечатку
(`get_by_username`), исправляется на этапе имплементации.

**PM user resolution в frontend**: проблема P3 из исследования (`/api/users/username/pm`
фильтрует по org звонящего) — требует отдельной проверки живьём перед
имплементацией, как уже существующие AI-роли работают.

### NestJS: Kafka consumer

Новый файл `packages/backend/src/kafka/kafka-consumer.service.ts`:

```typescript
import { Injectable, OnModuleInit, OnModuleDestroy, Logger, Inject } from '@nestjs/common';
import { Kafka, Consumer, EachMessagePayload } from 'kafkajs';
import { ConfigService } from '@nestjs/config';
import { SocketGateway } from '../socket/socket.gateway';

@Injectable()
export class KafkaConsumerService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(KafkaConsumerService.name);
  private kafka: Kafka;
  private consumer: Consumer;

  constructor(
    private configService: ConfigService,
    @Inject(forwardRef(() => SocketGateway))
    private socketGateway: SocketGateway,
  ) {}

  async onModuleInit() {
    this.kafka = new Kafka({
      clientId: 'webclient-nestjs',
      brokers: [this.configService.get('KAFKA_BROKERS', '10.0.0.2:9092')],
      retry: { retries: 10, initialRetryTime: 1000 },
    });
    this.consumer = this.kafka.consumer({
      groupId: `webclient-${process.env.HOSTNAME || 'default'}-${process.pid}`,
    });
    await this.consumer.connect();
    await this.consumer.subscribe({ topic: 'chat.events', fromBeginning: false });
    await this.consumer.run({
      eachMessage: async ({ message }: EachMessagePayload) => {
        try {
          const payload = JSON.parse(message.value.toString());
          const chatId = payload.chat_id;
          const msg = payload.message;
          this.socketGateway.broadcastToChat(chatId, msg);
        } catch (e) {
          this.logger.error(`Failed to handle chat.events message: ${e.message}`);
        }
      },
    });
    this.logger.log('Kafka consumer connected to chat.events');
  }

  async onModuleDestroy() {
    if (this.consumer) {
      await this.consumer.disconnect();
    }
  }
}
```

**Consumer group имя** уникально на инстанс (`hostname + pid`) — это
broadcast pattern: каждый инстанс NestJS получает все сообщения из `chat.events`,
потому что каждый обслуживает своих юзеров через WS. Если бы group был общий,
Kafka распределила бы partition'ы поровну, и только один инстанс получал бы
каждое сообщение — сломало бы доставку юзерам на других инстансах.

**`SocketGateway.broadcastToChat`:**

```typescript
public broadcastToChat(chatId: string, message: any): void {
  this.io.to(`chat:${chatId}`).emit(WsServerEvents.MESSAGE, message);
}
```

Простой public метод, вызывается из `KafkaConsumerService`.

### Frontend

Минимальные изменения:

1. В `POST /api/chats/{id}/messages` ответ теперь содержит
   `{user_message, ai_responses: [], agent_pending: boolean}`. Фронт уже
   обрабатывает массив ai_responses, поэтому пустой массив проблем не
   создаст.
2. Опционально — typing-индикатор: если `agent_pending=true`, показывать
   «AI думает…» в ленте. Скрывается при получении WS event `message` с
   `reply_to_id == user_message.id`.
3. Для PM-уведомлений — никаких изменений, существующий `message` event
   покрывает всё.

## EngineService wiring

```python
class EngineService:
    def __init__(self):
        # ... existing ...
        self.kafka_producer = KafkaProducerService()
        self.agent_run_storage = AgentRunStorage(self.postgres_dsn)
        # TaskNotificationService needs kafka_producer, chat_service, user_storage, message_storage
        self.task_notification_service = TaskNotificationService(
            chat_service=self.chat_service,
            message_storage=self.message_storage,
            user_storage=self.user_storage,
            kafka_producer=self.kafka_producer,
        )
        # TaskService gets task_notification_service injected
        self.task_service.task_notification_service = self.task_notification_service

        # Consumer for agent.requests (created later in initialize())
        self.agent_request_consumer: Optional[KafkaConsumerLoop] = None

    async def initialize(self):
        # ... existing init ...
        await self.agent_run_storage.init()
        await self.kafka_producer.start()

        self.agent_request_consumer = KafkaConsumerLoop(
            topic="agent.requests",
            group_id="engine-agent-runners",
            handler=self._handle_agent_request,
        )
        await self.agent_request_consumer.start()

    async def close(self):
        if self.agent_request_consumer:
            await self.agent_request_consumer.stop()
        await self.kafka_producer.stop()
        await self.agent_run_storage.close()
        # ... existing close ...
```

## Тестирование

### Engine pytest

1. **`test_task_notifications.py`** (mocked kafka_producer):
   - `test_notify_take_publishes_to_chat_events`
   - `test_notify_take_skips_legacy_task_no_creator`
   - `test_notify_accept_notifies_assignee`
   - `test_notify_reject_includes_comment`
   - `test_notify_overdue_notifies_both`
   - `test_notify_overdue_skips_duplicate_when_creator_is_assignee`
   - `test_pm_user_lookup_cached`

2. **`test_agent_runs_idempotency.py`**:
   - `test_mark_running_atomic_cas` — два consumer'а одновременно пытаются
     забрать request, только один получает True
   - `test_handle_agent_request_skips_already_done`
   - `test_handle_agent_request_marks_failed_on_exception`

3. **`test_kafka_bus.py`** (in-memory fake producer/consumer, без реального
   Kafka):
   - `test_producer_send_forwards_to_handler`
   - `test_consumer_does_not_commit_on_handler_error`
   - `test_cross_topic_isolation`

4. **`test_ai_service_async.py`**:
   - `test_process_ai_mentions_publishes_to_agent_requests`
   - `test_process_ai_mentions_returns_empty_ai_responses`
   - `test_process_ai_mentions_creates_agent_run_row`

### Integration (отдельный suite, требует Kafka)

- `tests/integration/test_kafka_end_to_end.py` — запускается только если
  `TEST_KAFKA_BROKERS` env var задан. Полный цикл: publish в `agent.requests`,
  consumer обрабатывает, publish в `chat.events`, второй consumer получает.

### NestJS jest

- `kafka-consumer.service.spec.ts` — мок `kafkajs`, проверка что
  `socketGateway.broadcastToChat` вызывается с корректными args для каждого
  type сообщения.

## Метрики успеха

1. PM-агент виден на главной странице WebClient как 4-й системный юзер.
2. Исполнитель берёт задачу → создатель получает сообщение в своём direct-чате
   с PM **в течение 1 секунды** (через WS, не F5).
3. При `@@pm` в любом чате → PM отвечает через ~5-10 секунд (время инференса),
   HTTP-запрос юзера возвращается мгновенно.
4. Рестарт Engine во время обработки запроса → после рестарта запрос подбирается
   заново из Kafka, не теряется. Idempotency через `agent_runs` не создаёт
   дубликат AI-сообщения.
5. Рестарт NestJS → активные WS отключаются, после переподключения сообщения
   догружаются через `GET /messages` (уже работает). Будущие сообщения идут
   live через новый KafkaConsumer.
6. `overdue` scheduler → обе стороны получают сообщения от PM.

## Зависимости

- ✅ п.1 базовая система задач
- ✅ п.8 отделы
- ✅ п.9 владение и переходы
- ✅ п.11 чаты задач и проектов (для будущего п.12)

## Открытые вопросы

### 1. Consumer-group для `chat.events` в NestJS — broadcast pattern

**Проблема:** Kafka consumer group нормально распределяет partition'ы между
членами group. Если два инстанса NestJS в одной group — Kafka отдаст partition
одному, другому нет. Но оба инстанса обслуживают разных юзеров через WS, и
оба должны получить событие.

**Решение в спеке:** каждый NestJS инстанс создаёт свою unique group
(`hostname + pid`). Это делает broadcast pattern: все инстансы получают всё.

**Альтернатива:** static group + inter-instance broadcast через Redis pub/sub
(тот же существующий Redis). Сложнее, но классичнее.

**Для Alpha:** простой broadcast через уникальные group'ы. После — переделаем
если понадобится.

### 2. Frontend typing indicator

Когда пользователь пишет `@@pm` и получает HTTP 202 с `agent_pending=true`,
показывать ли typing indicator в ленте?

**Варианты:**
- **А.** Показать «AI думает…» placeholder, скрыть при приходе WS-сообщения.
  Лучше UX, небольшое усложнение `MainChat.tsx`.
- **Б.** Не показывать ничего, AI-сообщение просто материализуется через
  5-10 сек. Проще, но юзер может подумать что команда не прошла.

**Рекомендация:** А. Небольшой state `pendingAgentMessages: Set<string>` в
MainChat, добавляется по `user_message.id` после HTTP 202, удаляется при
получении WS сообщения с `reply_to_id === user_message.id`.

### 3. Timeout agent run

Если agent_run зависает > 5 минут — что делать?

**Решение для Alpha:** нет встроенного timeout'а. Kafka retention `agent.requests`
— 24 часа, retry будет срабатывать если consumer'у долго не удаётся commit.

**Для production:** фоновая задача `check_stuck_agent_runs` каждую минуту
переводит `running` → `failed` для записей старше 5 минут, публикует
системное сообщение «AI не ответил» в `chat.events`.

### 4. PM resolution в frontend (проблема P3 из исследования)

`/api/users/username/pm` фильтрует по org звонящего. PM живёт в системной org.
Надо проверить как существующие AI-роли (`ai_qwen3` etc.) резолвятся — есть
ли fallback в `users_service.get_user_by_username` или это сломано на любой
системный username.

**Шаг 0 имплементации:** проверить это руками в браузере ДО любого кода.
Если сломано — первым коммитом добавить fallback в `users_service`.

### 5. Размер payload'а `prompt_context` в `agent.requests`

LangGraph запросы могут содержать большой контекст (история чата, RAG
результаты). Kafka default `max.message.bytes = 1MB`. Для большинства запросов
хватает, но агентные вызовы с RAG могут быть больше.

**Решение для Alpha:** проверить средний размер, если меньше 512KB — default
OK. Если больше — либо увеличить `max.message.bytes` до 5MB, либо хранить
контекст в Postgres и передавать только `user_message_id` + reference на
предыдущие сообщения, worker сам сформирует контекст.

**Рекомендация:** второе (референсный подход). Payload в Kafka — только
метаданные запроса. Сам текст восстанавливается worker'ом из БД. Это
единственный разумный путь для больших контекстов.

## Этапы имплементации

**Фаза 0 — Infrastructure + P3 проверка** (0.5 дня)
- Docker Compose для Kafka
- Проверка PM resolution в frontend (P3)
- Fallback в `users_service.get_by_username` если нужно

**Фаза 1 — Kafka basics + PM notifications** (1 день)
- `KafkaProducerService`, `KafkaConsumerLoop` в Engine
- Миграция 018 (PM role, system user, agent_runs таблица)
- `src/engine/prompts/pm.md`
- `TaskNotificationService` + интеграция в `TaskService`
- `chat.events` publisher подключён к TaskNotificationService
- NestJS `KafkaConsumerService` + подписка на `chat.events`
- Engine pytest для TaskNotificationService

**Фаза 2 — Async agent execution** (1.5 дня)
- `AgentRunStorage`
- Рефакторинг `AIService.process_ai_mentions` → publish в `agent.requests`
- Engine background consumer `agent.requests` с idempotency через
  `agent_runs.mark_running`
- Обновление HTTP response `POST /chats/{id}/messages`
- Engine pytest: async AIService, idempotency

**Фаза 3 — Frontend polish** (0.5 дня)
- `agent_pending` в HTTP response → typing indicator в MainChat
- Обновление tests во фронте
- Обновление моков в NestJS jest suite (HTTP response shape)

**Фаза 4 — Integration тесты** (0.5 дня)
- `test_kafka_end_to_end.py` с реальным Kafka
- Smoke через TestClient + локальный Kafka

**Итого: ~4 рабочих дня.**

## Риски

1. **Kafka connection pool / retry — aiokafka особенности.** Неправильный retry
   конфиг может привести к duplicate sends при транзиентных ошибках broker'а.
   Смягчение: `enable_idempotence=True` в producer, atomic CAS в `agent_runs`
   на consumer side.

2. **KRaft single-node — любой рестарт Kafka убивает active consumer'ов до
   reconnect.** Смягчение: `retry.retries=10` в обоих consumer'ах (Engine и
   NestJS), Kafka retention покрывает downtime.

3. **VPN между NestJS и Engine-Kafka.** Если VPN падает — NestJS consumer
   теряет соединение, пытается reconnect'ить. Пока VPN down — PM/AI сообщения
   не доставляются live, но копятся в Kafka с retention 1 час. После
   восстановления догоняется.

4. **Нагрузка agent_runs таблицы.** Каждый агентный вызов = строка в таблице.
   При активной работе — десятки тысяч в месяц. Нужен cleanup политика:
   DELETE `agent_runs` WHERE `finished_at < NOW() - 30 days` в scheduler.
