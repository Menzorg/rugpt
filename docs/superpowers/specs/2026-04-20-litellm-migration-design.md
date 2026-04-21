# Миграция LLM-инференса на LiteLLM proxy

Дата: 2026-04-20
Статус: design approved

## Мотивация

На Звере рядом с Ollama запущен vLLM и над ними LiteLLM proxy (OpenAI-совместимый шлюз на порту 4000). Цель — унифицировать все вызовы движка (генерация и эмбеддинги) через LiteLLM: одна точка входа, один API-ключ, один клиент; маршрутизация на Ollama / vLLM прячется за прокси и отвечает за выбор бэкенда по имени модели.

Дополнительный выигрыш — возможность впоследствии менять модели и провайдеров без правок кода движка: достаточно изменить `model_name` в ролях или добавить alias в LiteLLM-конфиг.

## Архитектура

Текущая:
```
engine → langchain-ollama (ChatOllama, OllamaEmbeddings)
       → http://localhost:11434 (Ollama напрямую)
```

Целевая:
```
engine → langchain-openai (ChatOpenAI, OpenAIEmbeddings)
       → http://zver:4000/v1 (LiteLLM proxy)
       → LiteLLM роутит по имени модели на Ollama / vLLM
```

Один клиент `langchain-openai`, один URL, один API-ключ. Пакет `langchain-ollama` выпиливается из зависимостей.

## Решения

1. **Именование env-переменных** — сохраняем `LLM_BASE_URL` (смена дефолта), добавляем новый `LLM_API_KEY`. Имена нейтральные, не привязанные к текущему прокси.
2. **Эмбеддинги** — тоже идут через LiteLLM. Требует от Александра настройки embedding-модели в LiteLLM-конфиге.
3. **Legacy `OllamaProvider`** — удаляется полностью (файл `src/engine/llm/providers/ollama.py`, директория `llm/` если остаётся пустой). Health-check упрощается до простого HTTP-probe на `LLM_BASE_URL + "/health/liveness"`.
4. **Имена моделей в БД** — обновляются миграцией 020 на канонические LiteLLM-имена: `google/gemma-4-31B-it` для генерации, `Qwen/Qwen3-Embedding-0.6B` для эмбеддингов.
5. **Подход к миграции** — big-bang replace. Один коммит, один деплой, откат через git revert.

## Config изменения

### `src/engine/config.py`

- `LLM_BASE_URL` — дефолт меняется с `http://localhost:11434` на `http://localhost:4000/v1`
- **Новая** `LLM_API_KEY` — дефолт `sk-dummy` (LiteLLM требует любой непустой заголовок `Authorization: Bearer …`)
- `DEFAULT_MODEL` — дефолт с `qwen2:0.5b` на `google/gemma-4-31B-it`
- `EMBEDDING_MODEL` — дефолт с `qwen3-embedding:0.6b` на `Qwen/Qwen3-Embedding-0.6B`
- `RAG_SUMMARY_MODEL` — остаётся с дефолтом `= DEFAULT_MODEL` (подтянется Gemma-4)

### `.env.example`

- Добавить `LLM_API_KEY=sk-dummy`
- Обновить значения `LLM_BASE_URL`, `DEFAULT_MODEL`, `EMBEDDING_MODEL` на новые дефолты

### `.env` на prod (RAG)

Правится вручную: `LLM_BASE_URL=http://192.168.1.XX:4000/v1`, `LLM_API_KEY=sk-dummy`.

## Код — точечные правки

### 1. `src/engine/agents/executor.py`

- Импорт `from langchain_ollama import ChatOllama` → `from langchain_openai import ChatOpenAI`
- Конструктор принимает `api_key: Optional[str] = None` (дефолт из `Config.LLM_API_KEY`)
- `_create_llm()` возвращает `ChatOpenAI(base_url=..., api_key=..., model=..., temperature=..., timeout=self.timeout)`

### 2. `src/engine/agents/graphs/simple.py`, `chain.py`, `multi_agent.py`

- Импорт `from langchain_ollama import ChatOllama` → `from langchain_openai import ChatOpenAI`
- Тип-хинты `ChatOllama` → `ChatOpenAI`

Логика графов не меняется: оба клиента реализуют интерфейс `BaseChatModel`, `.invoke()`/`.ainvoke()` работают одинаково.

### 3. `src/engine/agents/graphs/rule_generator.py`

- Заменить `ChatOllama(base_url=..., model=..., temperature=...)` на `ChatOpenAI(base_url=..., api_key=Config.LLM_API_KEY, model=..., temperature=...)`
- Добавить импорт `from ...config import Config`

### 4. `src/engine/services/rag_service.py`

- Импорт `from langchain_ollama import ChatOllama, OllamaEmbeddings` → `from langchain_openai import ChatOpenAI, OpenAIEmbeddings`
- Параметры `__init__`:
  - `ollama_embeddings_base_url`, `ollama_base_url` — убираются
  - Добавляется `llm_base_url: str`, `llm_api_key: str`
- Инстанцирование:
  - `self._embeddings = OpenAIEmbeddings(model=ollama_model, base_url=llm_base_url, api_key=llm_api_key)` (параметр `ollama_model` остаётся — через него передаётся `EMBEDDING_MODEL`)
  - `self._summary_llm = ChatOpenAI(model=Config.RAG_SUMMARY_MODEL, base_url=llm_base_url, api_key=llm_api_key, temperature=0)`

### 5. `src/engine/agents/tools/rag_tool.py`

- Импорт `from langchain_ollama.embeddings import OllamaEmbeddings` → `from langchain_openai import OpenAIEmbeddings`
- `_get_embeddings()` возвращает `OpenAIEmbeddings(model=Config.EMBEDDING_MODEL, base_url=Config.LLM_BASE_URL, api_key=Config.LLM_API_KEY)`

### 6. `src/engine/services/engine_service.py`

- При создании `RAGService` передавать `llm_base_url=Config.LLM_BASE_URL, llm_api_key=Config.LLM_API_KEY` вместо двух ollama-параметров
- `AgentExecutor(...)` — `base_url=Config.LLM_BASE_URL` (уже был), добавить `api_key=Config.LLM_API_KEY`
- Удалить импорт и использования `OllamaProvider`

### 7. `src/engine/tasks/ingest_queue.py`

- Передавать в `RAGService` те же новые параметры (`llm_base_url`, `llm_api_key`)

### 8. `src/engine/llm/providers/ollama.py` и родительские директории

- Удалить файл `ollama.py`
- Если `llm/providers/` и `llm/` остаются пустыми — удалить директории (в `engine_service.py` импортов из `llm/` больше не будет)

### 9. `src/engine/routes/health.py`

- Если был импорт `OllamaProvider` — заменить на простой `httpx.get(<root_url>/health/liveness, timeout=3.0)`, где `<root_url>` — `LLM_BASE_URL` с отрезанным суффиксом `/v1` (LiteLLM отдаёт health на корне, не под `/v1`).
- **Подтверждённое поведение LiteLLM** (curl'ом 2026-04-20): `GET /health/liveness` → `200 "I'm alive!"`, auth **не требуется**. `GET /health` → 401 (требует API-ключ) — этот endpoint не используем, он для админского мониторинга.
- Успех = HTTP 200 (тело не парсим). Точная реализация — задача плана.
- Эндпоинт `/api/v1/health/ready` проверяет: PostgreSQL + LiteLLM

## Миграция БД

Файл `src/engine/migrations/020_litellm_model_names.sql`:

```sql
-- Migration 020: Migrate role.model_name to LiteLLM canonical names.
-- All generation roles use Gemma-4 via vLLM through LiteLLM proxy.

UPDATE roles
SET model_name = 'google/gemma-4-31B-it',
    updated_at = NOW()
WHERE model_name IN ('qwen3:14b', 'gpt-oss:20b', 'qwen2.5:7b', 'glm-4.7-flash', 'qwen2:0.5b');
```

Идемпотентная: повторный запуск не делает ничего (WHERE не сработает). В prod БД сейчас активные роли используют `qwen3:14b` (три главных агента и `pm` из миграций 018/019).

## requirements.txt

- Добавить: `langchain-openai>=0.2.0`
- Удалить: `langchain-ollama>=0.2.0`
- `langchain`, `langchain-core`, `langchain-community`, `langchain-text-splitters` — остаются без изменений

## Тестирование

Запустить на dev-машине локально невозможно (нет доступа к Звере в том же виде), поэтому тестирование после деплоя на RAG:

1. `curl http://192.168.1.81:8100/api/v1/health/ready` → 200, `litellm: ok`
2. Залогиниться админом в webclient
3. Перейти в "Персональный ИИ" → "Размышлятор" → написать "привет, расскажи анекдот". Ожидание: приходит осмысленный ответ через LiteLLM → vLLM → Gemma
4. Загрузить маленький PDF через `/files` → проверить в БД `rag_status='indexed'` для этого файла → значит эмбеддинги работают
5. Открыть чат с "Поиск по документам" → запрос про содержимое загруженного PDF → должен найти и ответить с цитатой

## Rollback

Если LiteLLM не отвечает / Gemma выдаёт ерунду / где-то проблема:

1. `git revert` коммит миграции (на Маке)
2. `./deploy.sh` — rsync + рестарт движка с предыдущей версией
3. Миграцию 020 физически откатывать не надо — старые имена моделей восстанавливать не потребуется, т.к. код которому они были нужны (`langchain-ollama`) в откаченной версии снова использует эти имена

Если откатываем **только конфиг** (`.env`) без кода — не сработает, потому что код ожидает новый API. Откат — только целостный через git revert.

## Что не входит в эту миграцию

- Настройка самого LiteLLM и его `config.yaml` с aliases — сторона Александра
- Мониторинг LiteLLM/vLLM метрик (VRAM, latency) — отдельная задача (п.4 роадмапа)
- Streaming ответов от LLM — оставляется как есть (sync invoke), отдельная задача
- Нагрузочное тестирование Gemma против Qwen3 — отдельная задача (п.7 роадмапа)

## Зависимости

- Блокер: LiteLLM на Звере должен отвечать на `/v1/chat/completions`, `/v1/embeddings`, `/health/liveness`
- Модель `google/gemma-4-31B-it` должна быть загружена и доступна через LiteLLM на Звере — подтверждено curl'ом (2026-04-20)
- Модель `Qwen/Qwen3-Embedding-0.6B` должна быть доступна через LiteLLM на Звере — подтверждено curl'ом (2026-04-20)
- pgvector схема рассчитана на 1024-dim вектора; Qwen3-Embedding-0.6B выдаёт 1024 — совместимо
- Health endpoint: `/health/liveness` (подтверждено curl'ом 2026-04-20, отвечает `200 "I'm alive!"` без auth)

## Подтверждённые форматы запросов и ответов (curl на `http://192.168.1.80:4000`)

`GET /v1/models` → `{"data":[{"id":"google/gemma-4-31B-it","object":"model",...},{"id":"Qwen/Qwen3-Embedding-0.6B",...}],"object":"list"}`

`POST /v1/chat/completions` с `{"model":"google/gemma-4-31B-it","messages":[...]}` →
`{"id":"chatcmpl-...","created":...,"model":"google/gemma-4-31B-it","object":"chat.completion","choices":[{"finish_reason":"stop","message":{"content":"...","role":"assistant"}}],"usage":{...}}`

`POST /v1/embeddings` с `{"model":"Qwen/Qwen3-Embedding-0.6B","input":"..."}` →
`{"model":"Qwen/Qwen3-Embedding-0.6B","data":[{"index":0,"object":"embedding","embedding":[...1024 floats...]}],"object":"list","usage":{...}}`

Auth: `Authorization: Bearer sk-dummy` — любой непустой токен проходит для `/v1/*`. `/health/liveness` auth не требует, `/health` требует (но мы его не используем).
