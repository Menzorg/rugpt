# Разделение логов по компонентам (unified_logger)

> Задача от тимлида: до этого все логи писались в один файл — нечитаемо.
> Нужно разделить на файлы по компонентам (`routes.jsonl`, `services.jsonl`, …),
> с JSON Lines форматом, ротацией по дням и поддержкой `correlation_id` / `user_id`.

**Цель.** На каждый функциональный слой движка — отдельный JSONL-файл логов
в `logs/engine/<YYYY-MM-DD>/<component>.jsonl`. Чтобы `grep`/`jq` по слою
работал без шума, а старые папки можно было архивировать целиком.

**Подход.** Свой `unified_logger` поверх стандартного `logging`:
фабрика `get_logger(component)` возвращает кэшированный `EngUnifiedLogger`
с собственным `FileHandler` и `StructuredFormatter` (JSON). Контекст
запроса (correlation_id, user_id) — через `ContextVar`, ставится
middleware'ами на входе HTTP.

**Что НЕ входит:**
- Отправка логов в централизованную систему (Loki/ELK) — отдельная задача.
- Сэмплинг или rate-limit логов — пока пишем всё.
- Ротация по размеру файла (только по дате).

---

## Краткий обзор задач

| # | Задача | В двух словах | Статус |
|---|---|---|---|
| 1 | Базовый класс логгера | `BaseUnifiedLogger` (ABC), `StructuredFormatter`, `SafeJSONEncoder`. | ✅ done |
| 2 | Engine-вариант | `EngUnifiedLogger` — путь `logs/engine/<date>/`, префикс `rugpt.<component>`. | ✅ done |
| 3 | Фабрика-кэш | `get_logger(component)` с кэшем экземпляров (нет дубликатов FileHandler'ов). | ✅ done |
| 4 | Контекст запроса | `correlation_id_var` / `user_id_var` (ContextVar) + middleware. | ✅ done |
| 5 | Ротация по дате | thread-safe через `Lock` + double-checked locking. | ✅ done |
| 6 | Расстановка `get_logger("…")` | По всем слоям: routes, services, storage, agents, kafka, notifications, tasks, app. | ✅ done |
| 7 | Документация в CLAUDE.md | Описание формата, компоненты, примеры использования. | ✅ done |

---

## Структура файлов

| Файл | Роль |
|---|---|
| `src/engine/unified_logger/base.py` | `BaseUnifiedLogger` (ABC), `StructuredFormatter`, `SafeJSONEncoder`, `_check_and_rotate_handlers`. |
| `src/engine/unified_logger/eng.py` | `EngUnifiedLogger` — конкретная реализация, путь `logs/engine/<date>/`. |
| `src/engine/unified_logger/__init__.py` | Фабрика `get_logger(component)` + ре-экспорт `set_user_id` / `get_user_id`. |
| `src/engine/logging_context.py` | `correlation_id_var`, `user_id_var`, `CorrelationIDMiddleware`, `RequestLoggingMiddleware`. |
| `logs/engine/<YYYY-MM-DD>/<component>.jsonl` | Выходные файлы — один на компонент в день. |

---

## Архитектурные решения

- **JSON Lines.** Одна запись = одна строка JSON. Парсится `jq`, грепается
  по полям, легко импортируется в любой анализатор.
- **`SafeJSONEncoder`** — сериализует UUID/datetime/Decimal/set,
  fallback на `str(obj)`. Логгер не должен падать из-за нелитерального типа.
- **Ротация по дате.** На каждой записи сравниваем «сегодня» с датой
  активного handler'а. На смене суток — `Lock` + double-checked locking,
  закрываем старые FileHandler'ы, открываем новые в папке нового дня.
- **Кэш фабрики.** `get_logger("routes")` дважды возвращает один и тот же
  объект — иначе два FileHandler'а на один файл и дубли строк.
- **Контекст через ContextVar.** asyncio-friendly, корректно работает
  внутри одного запроса даже при `asyncio.gather`. `correlation_id` ставит
  middleware, `user_id` — `get_current_user` после JWT-валидации.
- **Сторонние логи в stdout.** uvicorn/asyncpg/httpx/aiokafka пишут через
  `logging.basicConfig` в stdout, в `<component>.jsonl` не попадают —
  отдельный поток, отдельная читабельность.

---

## Task 1: Базовый класс логгера ✅ done

`BaseUnifiedLogger(ABC)` с обязательным `_get_log_dir()` для подклассов.
`StructuredFormatter` собирает запись из `record.__dict__` + `extra`
+ контекста (correlation_id, user_id) и кодирует через `SafeJSONEncoder`.

---

## Task 2: Engine-вариант ✅ done

`EngUnifiedLogger(component)` фиксирует:
- путь `logs/engine/<YYYY-MM-DD>/`;
- имя файла `<component>.jsonl`;
- имя логгера `rugpt.<component>`.

---

## Task 3: Фабрика-кэш ✅ done

`get_logger(component)` хранит `Dict[str, EngUnifiedLogger]`. При повторном
вызове с тем же именем — тот же объект, тот же FileHandler.

---

## Task 4: Контекст запроса ✅ done

- `CorrelationIDMiddleware` — читает header `X-Correlation-ID` / query
  `correlation_id` / генерирует UUID; кладёт в `correlation_id_var`.
- `RequestLoggingMiddleware` — логирует начало/конец запроса
  (метод, путь, статус, длительность).
- `routes/auth.py:get_current_user` — после JWT-валидации ставит
  `user_id` через `set_user_id()`.

---

## Task 5: Ротация по дате ✅ done

`_check_and_rotate_handlers` сравнивает `today` с `mCurrentDate`. На смене
суток — `threading.Lock` + double-checked locking, чтобы две корутины
не пересоздали handler одновременно. Старый handler закрывается, новый
открывается в новой папке.

---

## Task 6: Расстановка `get_logger("…")` ✅ done

Все слои используют свой компонент:
- `get_logger("routes")` — во всех `src/engine/routes/*.py`;
- `get_logger("services")` — в `src/engine/services/*.py`;
- `get_logger("storage")` — в `src/engine/storage/*.py`;
- `get_logger("agents")` — в `src/engine/agents/*.py`;
- `get_logger("kafka")` — в `src/engine/kafka/*.py`;
- `get_logger("notifications")` — в `src/engine/notifications/*.py`;
- `get_logger("tasks")` — в `src/engine/services/task_*.py`;
- `get_logger("app")` — в `src/engine/app.py`;
- `get_logger("request")` — в `RequestLoggingMiddleware`.

Проверено: в `logs/engine/2026-05-13/` лежат `routes.jsonl`, `services.jsonl`,
`storage.jsonl`, `kafka.jsonl`, `notifications.jsonl` (и тестовые smoke-файлы).

---

## Task 7: Документация в CLAUDE.md ✅ done

Раздел «Логи» в `CLAUDE.md` описывает:
- путь и формат файлов;
- список компонентов;
- пример использования (`logger.info("user logged in", user_id=42)`);
- источники `correlation_id` / `user_id`;
- внутренюю архитектуру (base/eng/__init__).

---

## Self-review

**Покрытие исходного запроса:**
- ✅ Разделение по компонентам → Task 6 (фабрика + расстановка).
- ✅ JSON-формат → Task 1 (`StructuredFormatter` + `SafeJSONEncoder`).
- ✅ Ротация по дате → Task 5.
- ✅ Корреляция запросов → Task 4 (`correlation_id` + middleware).
- ⛔ Централизованная отправка (Loki/ELK) → ВНЕ scope, отдельной задачей при необходимости.
- ⛔ Сэмплинг/rate-limit → ВНЕ scope.

**Возможные риски:**
- Очень тяжёлый объект в `**kwargs` (например, целиком `User` со связями) —
  `SafeJSONEncoder` сделает `str(obj)`, но строка может оказаться огромной.
  При необходимости — добавить max-length, не сейчас.
- Если процесс не успевает закрыть FileHandler'ы перед SIGKILL — последняя
  строка может остаться без `\n`. Принимаемо для JSONL (читатель пропустит).

---

## Рекомендуемый порядок

Все 7 задач сделаны. Этот файл — отчётный, оставляем для истории.
Если позже потребуется расширение (новый компонент, новый источник
контекста) — добавлять отдельным таском.
