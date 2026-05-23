# `/health/ready` — реальная проверка зависимостей

> Задача от тимлида: текущий `/health/ready` проверяет только LiteLLM
> и не смотрит на БД. Это полу-fake readiness: оркестратор будет считать
> сервис готовым даже когда asyncpg-пул мёртв и любой запрос упадёт 500-кой.

**Цель.** `/health/ready` должен возвращать `ready=true` только когда
все критические зависимости отвечают. По каждой зависимости — отдельный
статус в ответе, чтобы видеть какая именно down.

**Подход.** Параллельные пробы через `asyncio.gather(..., return_exceptions=True)`,
у каждой свой таймаут. Общий бюджет на эндпоинт — 3 секунды, чтобы
не блокировать Kubernetes readiness probe. Ответ — JSON со статусом
каждой зависимости + общий булев `ready`.

**Что НЕ входит:**
- Метрики (Prometheus): отдельная задача.
- `/health/startup` (отдельная probe для долгих init'ов): пока не нужен.
- Алерты на основе `/ready`: задача мониторинга, не Engine.

---

## Краткий обзор задач

| # | Задача | В двух словах | Статус |
|---|---|---|---|
| 1 | Решить состав проверок | Какие зависимости считать «критическими» для `ready`. | ✅ done (согласовано тимлидом) |
| 2 | Проба БД | `SELECT 1` на `EngineService.pg_pool` с таймаутом. | ✅ done |
| 3 | Проба Kafka (если включена) | Lightweight metadata fetch у producer'а; no-op если `KAFKA_ENABLED=false`. | ✅ done |
| 4 | Проба vLLM | Обернуть существующий `_litellm_alive` в формат `_check_vllm`. | ✅ done |
| 5 | Структура ответа | Свести в общий JSON, выставить HTTP 503 при `ready=false`. | ✅ done |
| 6 | Smoke-проверка | Подменить `LLM_BASE_URL` на мёртвый, проверить `vllm.status=fail` + 503. | ✅ done |

---

## Структура файлов

| Файл | Роль |
|---|---|
| `src/engine/routes/health.py` | Добавить пробы `_db_alive`, `_kafka_alive`, расширить `readiness_check`. |
| `src/engine/services/engine_service.py` | Возможный новый helper `ping()` для проб БД (если решим прятать `pg_pool` внутри). |
| `src/engine/kafka/producer.py` | Helper `is_alive()` (metadata-fetch с таймаутом) — если решим проверять Kafka. |

`config.py` — без изменений; `KAFKA_ENABLED` уже есть.

---

## Архитектурные решения

> Состав критичных зависимостей утверждён тимлидом, см. `~/health-ready-task.md`.

- **Что критично для `ready`.** PostgreSQL, vLLM (через LiteLLM), Kafka
  (если включена). Без любой из них пользовательский запрос не
  отрабатывает → должен уехать в `not ready`.
- **vLLM (через LiteLLM).** Тимлид: «это и есть мозг движка, без неё
  AI-ответы не работают» — **блокирует** `ready`. (Ранее в этом таске
  было «не блокирует» — решение пересмотрено тимлидом.)
- **Kafka.** Если `KAFKA_ENABLED=true` — критично (агентные запросы
  без неё стоят). Если `false` — проба скипается, в `ready` это
  ошибкой не считается.
- **Redis.** В `config.py` объявлены env-переменные `REDIS_*`, но ни
  один модуль в `src/engine` Redis реально не использует (проверено
  grep'ом). Пробу не добавляем; если Redis позже подключат — отдельная
  задача.
- **Scheduler / LangChain / агенты.** В `/ready` не проверяем — это код,
  «жив» по факту живого процесса. См. `/live`.
- **Параллельно + таймауты.** Каждой пробе свой `asyncio.wait_for(..., timeout=2.0)`.
  `asyncio.gather(return_exceptions=True)` — одна упавшая проба не валит остальные.
- **Формат checks[*]:** `{"status": "ok"|"fail", "duration_ms": int, "error": "..." (optional)}`
  — оператор сразу видит, кто именно лёг и за сколько ответил.
- **HTTP 503 при not ready.** Стандартное поведение для Kubernetes
  readiness probe — статус-код важнее тела ответа.
- **Никаких write-проб.** Только чтение/ping. `/health/ready` не должен
  иметь побочных эффектов.

---

## Task 1: Решить состав проверок ✅ done

Зафиксировать таблицу «зависимость → критична для ready / показывается
в ответе» одним абзацем в комментарии к `readiness_check`.

| Зависимость | Критична? | Чем проверяем |
|---|---|---|
| PostgreSQL | да | `SELECT 1` через `pg_pool.acquire()` |
| vLLM (через LiteLLM) | да | существующий `_litellm_alive` (HTTP health endpoint) |
| Kafka (если `KAFKA_ENABLED=true`) | да | `producer.client.fetch_all_metadata()` |
| Redis | — не используется в `src/engine`, пробы нет | — |

- [x] **1.1** Согласовать состав с тимлидом. Решение: PG / vLLM / Kafka —
  обязательные. Источник: `~/health-ready-task.md`.
- [x] **1.2** Проверить факт использования Redis в `src/engine` — grep
  показал только env-переменные в `config.py`, реальных вызовов нет.
  Пробу не добавляем.

---

## Task 2: Проба БД ✅ done

**Файл:** `src/engine/routes/health.py`.

```python
async def _check_postgres() -> dict:
    """Ping Postgres via shared pool. Returns dict with status/duration_ms/error."""
    start = time.perf_counter()
    engine = get_engine_service()
    # NB: пулы живут в каждом storage (BaseStorage.pg_pool). Все используют
    # один DSN — берём любой репрезентативный, чтобы проверить, что PG жив.
    pool = engine.user_storage.pg_pool if engine else None
    if pool is None:
        return {"status": "fail", "duration_ms": 0, "error": "pg_pool not initialized"}
    try:
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"postgres health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}
```

- [x] **2.1** Импорты `asyncio`, `time`, `get_engine_service` добавлены.
- [x] **2.2** `_check_postgres()` написан в `src/engine/routes/health.py`.
  Пул берётся как `engine.user_storage.pg_pool` (у `EngineService` нет
  прямого `pg_pool` — пулы живут per-storage в `BaseStorage`).
- [x] **2.3** Ветка `pool is None` — отдельный ранний return с
  `error="pg_pool not initialized"`. Объяснение: `pool.acquire()`
  на `None` дал бы `AttributeError`, который `except Exception` хоть
  и поймал бы, но `error` стал бы загадочным
  `'NoneType' object has no attribute 'acquire'` — оператор не поймёт
  что упало. Явная ветка возвращает осмысленный `error` для дежурного.

---

## Task 3: Проба Kafka ✅ done

Только если `Config.KAFKA_ENABLED`. Иначе возвращаем
`{"status": "skipped", "duration_ms": 0}` — в `ready` считается как успех.

```python
async def _check_kafka() -> dict:
    if not Config.KAFKA_ENABLED:
        return {"status": "skipped", "duration_ms": 0}
    start = time.perf_counter()
    engine = get_engine_service()
    producer = engine.kafka_producer if engine else None
    if producer is None:
        return {"status": "fail", "duration_ms": 0, "error": "producer not initialized"}
    try:
        await asyncio.wait_for(producer.ping(), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        return {"status": "ok", "duration_ms": duration_ms}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"kafka health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}
```

- [x] **3.1** `KafkaProducerService.ping()` добавлен в `src/engine/kafka/producer.py`.
  Использует `self._producer.client.fetch_all_metadata()`. Бросает
  `RuntimeError` если продьюсер выключен или не стартовал — caller (route)
  ловит через общий `except Exception`. Разделение слоёв: infra бросает,
  route форматирует JSON.
- [x] **3.2** `_check_kafka()` в `health.py`. Доступ к продьюсеру через
  `engine.kafka_producer` (атрибут `EngineService`, не per-storage как у БД).
- [x] **3.3** `kafka.status ∈ {"ok", "fail", "skipped"}`. `skipped` —
  ранний выход в начале функции, до таймера, чтобы `duration_ms=0`
  отражал «не пробовали».

---

## Task 4: Проба vLLM ✅ done

Тимлид: «Поход на её health endpoint уже сделан в коде (`_litellm_alive`).
Оставь логику, заверни в общий механизм». То есть переиспользуем
существующую функцию, но возвращаем уже в новом формате
`{status, duration_ms, error?}` — как `_check_postgres` / `_check_kafka`.

```python
async def _check_vllm() -> dict:
    start = time.perf_counter()
    try:
        ok = await asyncio.wait_for(_litellm_alive(), timeout=2.0)
        duration_ms = int((time.perf_counter() - start) * 1000)
        if ok:
            return {"status": "ok", "duration_ms": duration_ms}
        return {"status": "fail", "duration_ms": duration_ms, "error": "litellm not alive"}
    except Exception as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        logger.warning(f"vllm health check failed: {e}")
        return {"status": "fail", "duration_ms": duration_ms, "error": str(e)}
```

- [x] **4.1** `_litellm_alive` возвращает `bool`, исключения ловит
  внутри (httpx-таймаут / любой `Exception` → `False`). Значит наш
  внешний `wait_for` сработает только если она зависнет дольше 2с.
- [x] **4.2** `_check_vllm()` добавлен в `health.py`. Адаптер: вызывает
  `_litellm_alive()` → конвертирует `bool` в `{status, duration_ms, error?}`.
  Две ветки fail: `ok is False` → `error="litellm not alive"`;
  `Exception` (таймаут) → `error=str(e)`.
- [x] **4.3** Callsites `_litellm_alive` проверены грепом — есть только
  определение и один вызов в текущем `readiness_check` (его перепишем
  в Task 5). Никаких внешних потребителей; функцию не трогаем.

---

## Task 5: Структура ответа + HTTP 503 ✅ done

Каждая проба возвращает `dict`:
`{"status": "ok" | "fail", "duration_ms": int, "error": "..." (optional)}`.
Имена ключей в `checks`: `postgres`, `vllm`, `kafka`.

```python
@router.get("/ready")
async def readiness_check(response: Response):
    postgres, vllm, kafka = await asyncio.gather(
        _check_postgres(), _check_vllm(), _check_kafka(),
        return_exceptions=False,   # exceptions ловим внутри проб
    )
    ready = (
        postgres["status"] == "ok"
        and vllm["status"] == "ok"
        and kafka["status"] in ("ok", "skipped")
    )
    if not ready:
        response.status_code = 503
        logger.warning(
            "readiness_check failed",
            postgres=postgres["status"], vllm=vllm["status"], kafka=kafka["status"],
        )
    return {
        "ready": ready,
        "checks": {"postgres": postgres, "vllm": vllm, "kafka": kafka},
        "timestamp": datetime.utcnow().isoformat(),
    }
```

Пример успешного ответа:
```json
{
  "ready": true,
  "checks": {
    "postgres": {"status": "ok", "duration_ms": 3},
    "vllm":     {"status": "ok", "duration_ms": 45},
    "kafka":    {"status": "ok", "duration_ms": 12}
  },
  "timestamp": "..."
}
```

`duration_ms` мерим вокруг `asyncio.wait_for(...)` через
`time.perf_counter()` — внутри каждой `_check_*`.

- [x] **5.1** `readiness_check` переписан с параметром `response: Response`
  (DI от FastAPI). HTTP 503 через `response.status_code = 503`, тело
  ответа сохраняется.
- [x] **5.2** Импорт `Response` добавлен в `from fastapi import ...`.
- [x] **5.3** `asyncio.gather(_check_postgres(), _check_vllm(), _check_kafka(),
  return_exceptions=False)` — параллельный запуск. `False` явно, т.к.
  `_check_*` сами ловят исключения внутри и всегда возвращают dict.
- [x] **5.4** Логика `ready`: postgres и vllm — строго `"ok"`; kafka —
  `"ok"` или `"skipped"` (выключенная не считается ошибкой).
- [x] **5.5** `logger.warning("readiness_check failed", postgres=...,
  vllm=..., kafka=...)` — kwargs летят в `metadata` JSON-лога, оператор
  сразу видит, что упало, в `routes.jsonl`.

---

## Task 6: Smoke-проверка ✅ done

Тимлид: «БД сам не положишь — прав нет». Поэтому основной сценарий
падения проверяем через vLLM (`LLM_BASE_URL` на мёртвый адрес). Для PG
достаточно убедиться, что здоровая система отвечает `ok` с разумным
`duration_ms`.

- [x] **6.1** Поднят локально, `curl /api/v1/health/ready` — HTTP 503,
  postgres/kafka `ok` с измерениями, vllm `fail` (см. ниже про Ollama).
  Параллельность работает: total ~80мс при пробных duration_ms 63/66/63.
- [x] **6.2** vLLM-fail сценарий получен «бесплатно» — у пользователя
  в `.env` `LLM_BASE_URL=http://localhost:11434/v1` (Ollama, а не LiteLLM).
  Ollama не имеет `/health/liveness` (отдаёт 404), поэтому
  `_litellm_alive` нативно возвращает `False`. JSON-ответ:
  `{"vllm": {"status": "fail", "duration_ms": 66, "error": "litellm not alive"}}`.
- [x] **6.3** `KAFKA_ENABLED=false` через shell-env (без правки `.env`,
  т.к. `load_dotenv` без `override=True` пропускает существующие env-vars).
  Ответ: `{"kafka": {"status": "skipped", "duration_ms": 0}}`. `ready`
  остался `false` только из-за vllm — kafka не блокирует.
- [x] **6.4** Warning-логи: `routes.jsonl` содержит записи
  `{"message": "readiness_check failed", "metadata": {"postgres": "ok",
  "vllm": "fail", "kafka": "ok"/"skipped"}}` с `correlation_id`. Уровень
  WARNING, не ERROR.

**Известное расхождение со спекой (не баг нашей задачи):** у пользователя
в `.env` стоит Ollama напрямую, а не LiteLLM. `_litellm_alive` зашит на
`/health/liveness` (это endpoint LiteLLM, не Ollama). На дев-машине с
Ollama vLLM-проба будет всегда `fail`. На прод-окружении с LiteLLM или
vLLM-OpenAI-compat сервером с этим endpoint'ом — будет `ok`. Это
существующая особенность `_litellm_alive` (мы её только обернули), не
наш баг. Если бизнес скажет «работать через Ollama напрямую» —
отдельный таск по доработке probe-endpoint'а.

**Healthy-сценарий (`ready=true`) на дев-машине не воспроизводим без
подмены `_litellm_alive` или поднятия LiteLLM-прокси. В прод-окружении
ожидается работать из коробки.**

---

## Self-review

**Покрытие исходного запроса (тимлид, `~/health-ready-task.md`):**
- ✅ Проверка PostgreSQL → Task 2.
- ✅ Проверка vLLM как обязательной → Task 4.
- ✅ Проверка Kafka с условием `KAFKA_ENABLED` → Task 3.
- ✅ Корректный HTTP-статус для оркестратора → Task 5.
- ✅ Параллельность + таймауты + WARNING-логирование → Task 5.
- ✅ Redis: проверено, в `src/engine` не используется → не добавляем.
- ✅ Smoke-сценарий по vLLM (БД не уронить — прав нет) → Task 6.

**Возможные риски:**
- Если `EngineService` ещё не инициализирован к моменту запроса
  (startup race) — `_check_postgres` вернёт `status="fail"` с явным
  `error="pg_pool not initialized"`. Это ровно то поведение, которое
  нужно для readiness probe.
- Таймауты 3×2с — `asyncio.gather` параллелит → общий бюджет ~2 с,
  не более 3 с с накладными.
- `producer.ping()` через `fetch_all_metadata` может быть тяжеловатым
  на больших кластерах. Для single-node в текущем setup'е — копейки.
- `_litellm_alive` уже существует — перед обёрткой грепнуть callsites,
  чтобы не сломать другие health-роуты (см. **4.3**).

---

## Рекомендуемый порядок

1. ~~**Task 1**~~ — закрыто, состав согласован с тимлидом.
2. **Task 2** — проба БД, минимальный кусок, после которого `/ready`
   уже становится полезным.
3. **Task 3** — Kafka.
4. **Task 4** — vLLM (обёртка существующего `_litellm_alive`).
5. **Task 5** — собрать в новый ответ + статус 503.
6. **Task 6** — smoke, доказательства в PR.

---

## Известные проблемы (для PR-описания / backlog'а)

Найдено при self-review после закрытия Task 6. Это **не баги нашего
кода** — это операционный риск и пре-existing проблема, на которые
смотрит вся работа `/ready` целиком. Записать тимлиду в PR.

### Проблема 1: pool starvation под нагрузкой (риск каскадного отказа)

Все три PG-пробы (а сейчас она одна — `_check_postgres`) идут через
**общий пул** `engine.user_storage.pg_pool`, через который ходит и
реальный пользовательский трафик. `BaseStorage` открывает пул с
`max_size=10` коннектов.

**Сценарий**: под нагрузкой все 10 коннектов заняты живыми
запросами. Приходит k8s readiness probe → `pool.acquire()` ждёт
свободный коннект → не дожидается за 2 секунды → `TimeoutError` →
`postgres="fail"` → `/ready` отдаёт 503 → k8s **выводит здоровый,
но загруженный pod из балансировки**. Трафик перераспределяется на
остальные → они тоже захлёбываются → каскадный отказ.

**Это классический failure mode** health-чеков, идущих через общий
пул. На single-node dev машине не воспроизводится; в проде под
реальной нагрузкой — критично.

**Вердикт тимлида: игнорируем на уровне приложения.** Будет решено
инфраструктурно — pooler (PgBouncer/аналог) перед PG. Отдельный пул
для health-проб в коде Engine **не пилим**. Пункт закрыт как
`wontfix (infra)`.

### Проблема 2: `_litellm_alive` строит root_url из пути — решать по построению

```python
root_url = Config.LLM_BASE_URL.removesuffix("/v1").removesuffix("/")
```

Ручная срезка хвоста не покрывает варианты вроде `…/v1/` или
`…/openai/v1` — и **никогда не закроет**, потому что список форм URL
неограничен.

**Вердикт тимлида (см. `~/health-ready-task.md`, раздел «Что значит
решить проблему по-настоящему»):** перестановка `removesuffix` —
заплатка, не решение. Признаки заплатки: «покрывает все N вариантов»,
правка в той же ручной обработке, защита в точке потребления (на
каждом вызове пробы), защита от данных, которые сами контролируем
(`.env` — не пользовательский ввод).

**Правильный фикс — убрать нормализацию, а не допиливать.** Здоровье
LiteLLM живёт в корне сервера и от пути `/v1` не зависит. Значит
выводим root из структуры URL, путь в вычислении не участвует:

```python
from urllib.parse import urlsplit
p = urlsplit(Config.LLM_BASE_URL)
root_url = f"{p.scheme}://{p.netloc}"   # host:port — одинаков для всех форм пути
```

Делать это **один раз при загрузке `Config`** (нормализация в
источнике), а не в `_litellm_alive` на каждом вызове.

**Это НЕ backlog-таск «допилить нормализацию»**, а удаление ручной
срезки. Объём — несколько строк, делаем сразу. Шаги:
- [x] В `config.py` вывести `LLM_ROOT_URL` через `urlsplit` при загрузке
  (`config.py:85-86`).
- [x] В `_litellm_alive` заменить `.removesuffix(...).removesuffix(...)`
  на `Config.LLM_ROOT_URL` (`health.py:23`).
- [x] Проверить остальные потребители `LLM_BASE_URL` — не сломать
  инференс. Греп: `engine_service.py` (×3), `ingest_queue.py`,
  `analyze_image.py` получают полный URL с `/v1` как раньше; срезку
  делал только `health.py` — он и переключён на root.

**✅ Решено (2026-05-22).** Smoke: `BASE=http://localhost:11434/v1` →
`ROOT=http://localhost:11434`, проба бьёт в
`http://localhost:11434/health/liveness`. Ручная срезка удалена,
не переставлена.
