# Inference Server: SGLang + LiteLLM

Замена Ollama на SGLang + LiteLLM для serving моделей на GPU-сервере Zver.

**Дата:** 2026-04-03
**Статус:** draft
**Компонент:** инфраструктура Zver (GPU-сервер)

---

## Контекст и проблема

Сейчас Zver (~200GB VRAM) использует Ollama для inference. Ollama — обёртка над llama.cpp, спроектированная для локальной разработки, не для production:

- **Нет настоящего batching.** При параллельных запросах дублирует inference-контексты. 10 запросов = 10x память на буферы. Throughput не растёт, пользователи ждут в очереди.
- **41 tok/s под нагрузкой** vs 793 tok/s у vLLM (бенчмарк Red Hat, та же модель, то же железо).
- **P99 TTFT 673ms** vs 80ms у vLLM. Каждый десятый запрос — ощутимая задержка.
- **Ограниченный параллелизм.** Дефолт: 4 параллельных запроса. С тюнингом до 32 — ITL становится нестабильным, спайки латентности.
- **Базовый tool calling.** Нестабильный structured output.
- **Только GGUF формат.** Нет прямой поддержки HuggingFace safetensors.

### Требования

1. **Производительность** — continuous batching, efficient memory management, high throughput.
2. **Параллельные запросы** — десятки concurrent запросов без деградации.
3. **Функциональность** — tool calling, structured output, multi-model serving, квантизация.
4. **Совместимость** — OpenAI-compatible API, чтобы Engine (FastAPI) не менял код.

---

## Исследование альтернатив

### Иерархия производительности

```
TensorRT-LLM > SGLang >= LMDeploy > vLLM >> TGI >> llama.cpp > Ollama
```

### Рассмотренные варианты

| Критерий | Ollama | llama.cpp | vLLM | SGLang | TensorRT-LLM |
|----------|--------|-----------|------|--------|---------------|
| Throughput (Llama 8B, H100) | ~41 tok/s | ~80-100 tok/s | ~12,500 tok/s | ~16,200 tok/s | ~2500-4000 tok/s |
| Continuous batching | Нет | Нет | Да | Да | Да |
| Prefix caching hit rate | Нет | Нет | 10-20% | 75-90% | Нет |
| Concurrent до деградации | ~4 | ~8 | 100-150 | 100-150 | 200+ |
| GPU utilization | Низкий | Низкий | 85-92% | 85-92% | 95%+ |
| Memory waste | 60-80% | 60-80% | <4% | <4% | <4% |
| Tool calling | Базовый | Нет | Да | Да (нативный) | Да |
| Structured output | Нет | Нет | Guided decoding | FSM (3x быстрее) | Да |
| Multi-GPU | Нет | Нет | TP/PP/EP | TP/PP/EP/DP | TP/PP/EP |
| Формат моделей | GGUF | GGUF | HF, GGUF | HF, GGUF | TensorRT (компиляция) |
| Квантизация | GGUF quants | GGUF quants | FP8/FP4/AWQ/GPTQ | FP4/FP8/INT4/AWQ/GPTQ | FP8/FP4/INT8 |
| OpenAI API | Да | Нет | Да | Да | Да (через Triton) |
| Лицензия | MIT | MIT | Apache 2.0 | Apache 2.0 | Apache 2.0 |
| Сложность настройки | Минимальная | Средняя | Средняя | Средняя | Высокая |
| Production readiness | Dev only | Edge/CPU | Production | Production | Enterprise |

### Отсеянные

- **Ollama** — не справляется с concurrent нагрузкой, нет batching.
- **llama.cpp** — нет continuous batching, нет multi-GPU, нет managed memory.
- **TensorRT-LLM** — требует компиляцию каждой модели в TensorRT engine, сложная настройка окружения, vendor lock-in. Оправдан при сотнях concurrent запросов и стабильном наборе моделей. Overkill сейчас.
- **TGI** — HuggingFace перевёл в maintenance mode (декабрь 2025), рекомендует vLLM или SGLang.
- **Aphrodite** — форк vLLM с экзотическими sampling-стратегиями. Нишевый, AGPL лицензия.
- **LocalAI** — не inference engine, а обёртка. Производительность = бэкенд внутри.

### Финалисты: SGLang vs vLLM

Оба production-ready, оба OpenAI-compatible, оба поддерживают multi-GPU и квантизацию.

**SGLang выигрывает для RuGPT по трём причинам:**

1. **RadixAttention.** Роли (lawyer, accountant, hr) имеют длинные system prompt'ы. Когда несколько пользователей вызывают одну роль — все запросы начинаются с одного prefix'а. SGLang хранит вычисленный KV-кеш в дереве и переиспользует (hit rate 75-90%). vLLM пересчитывает каждый раз (hit rate 10-20%). Это прямая экономия latency и VRAM.

2. **Structured output 3x быстрее.** Compressed FSM для constrained decoding. Критично для tool calling агентов (calendar_create, rag_search).

3. **29% быстрее на throughput** в batch inference бенчмарках на H100.

**vLLM выигрывает по:**
- Более зрелое community и документация.
- Больше production deployments в дикой природе.

**Решение:** SGLang. Разница в зрелости сократилась (LMSYS — серьёзная команда, NVIDIA включила в документацию DGX Spark). Архитектурные преимущества для агентной системы перевешивают.

---

## Архитектура решения

### Три слоя

```
Engine (FastAPI, 5090)
    │
    │  HTTP (LAN), OpenAI-compatible API
    │  model="qwen2.5:7b" / "qwen2.5:72b" / "qwen3-embedding"
    ▼
┌─────────────────────────────────────────────────────────┐
│  LiteLLM Proxy (:4000)              Zver               │
│                                                         │
│  Маршрутизация по model name:                           │
│    "qwen2.5:7b"     → localhost:30000                   │
│    "qwen2.5:72b"    → localhost:30001                   │
│    "qwen3-embedding" → localhost:30002                   │
│                                                         │
│  + health checks, retry, fallback, logging              │
└────────┬──────────────────┬──────────────────┬──────────┘
         │                  │                  │
    ┌────▼──────┐     ┌─────▼──────┐     ┌────▼───────┐
    │ SGLang #1 │     │ SGLang #2  │     │ SGLang #3  │
    │ :30000    │     │ :30001     │     │ :30002     │
    │           │     │            │     │            │
    │ qwen2.5   │     │ qwen2.5    │     │ qwen3-emb  │
    │ 7B        │     │ 72B        │     │ 0.6B       │
    │ GPU 0     │     │ GPU 1-3    │     │ GPU 0 (sh) │
    │           │     │ (tp=4)     │     │            │
    │ Простые   │     │ Сложные    │     │ Embedding  │
    │ роли      │     │ задачи     │     │ для RAG    │
    └───────────┘     └────────────┘     └────────────┘
```

### Зоны ответственности

| Слой | Делает | Не делает |
|------|--------|-----------|
| **SGLang** | GPU inference, continuous batching, KV-кеш, RadixAttention, structured output | Маршрутизация между моделями, fallback, мониторинг |
| **LiteLLM** | Маршрутизация model→instance, health checks, fallback, load balancing, rate limiting, logging | Inference, работа с GPU |
| **Engine** | Бизнес-логика, агенты, промпты, выбор модели через `role.model_name` | Знает только model name и endpoint URL |

Каждый слой делает одну вещь. Замена любого слоя не ломает остальные.

---

## SGLang: детали

### Что делает каждый instance

```
Входящий запрос
    │
    ▼
RadixAttention Cache
    │
    │  Дерево prefix'ов:
    │  "Ты юрист компании..." → CACHE HIT (не пересчитываем prefix)
    │    ├── "проверь договор"
    │    ├── "срок давности?"
    │    └── "напиши претензию"
    │
    ▼
Continuous Batching
    │
    │  Запросы не ждут в очереди.
    │  Новый запрос подхватывается в текущий batch на лету.
    │  batch = [req1, req2] → GPU step → +req3 подъехал →
    │  batch = [req1, req2, req3] → следующий step
    │
    ▼
Paged KV-Cache
    │
    │  VRAM выделяется страницами (как виртуальная память).
    │  Нет фрагментации. <4% waste vs 60-80% у наивных подходов.
    │  → Больше запросов помещается в GPU одновременно.
    │
    ▼
Ответ
```

### Конфигурация instances

```bash
# Instance 1: лёгкая модель для быстрых ответов
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct \
  --port 30000 \
  --tp 1 \
  --mem-fraction-static 0.85 \
  --chunked-prefill-size 8192

# Instance 2: тяжёлая модель, 4 GPU через tensor parallelism
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-72B-Instruct \
  --port 30001 \
  --tp 4 \
  --quantization fp8 \
  --mem-fraction-static 0.90

# Instance 3: embedding модель для RAG
python -m sglang.launch_server \
  --model-path Qwen/qwen3-embedding-0.6b \
  --port 30002 \
  --tp 1 \
  --is-embedding
```

### Ключевые параметры

| Параметр | Назначение | Значение |
|----------|------------|----------|
| `--tp N` | Tensor parallelism — разбить модель на N GPU | 1 для 7B, 4 для 72B |
| `--quantization` | Квантизация весов | `fp8` для экономии VRAM |
| `--mem-fraction-static` | Доля VRAM под KV-кеш | 0.85-0.90 |
| `--chunked-prefill-size` | Размер chunk при prefill | 8192 |
| `--max-running-requests` | Лимит concurrent запросов | По умолчанию авто |
| `--is-embedding` | Режим embedding модели | Для RAG |

---

## LiteLLM: детали

### Конфигурация

```yaml
# litellm_config.yaml

model_list:
  # Лёгкая модель — быстрые ответы
  - model_name: "qwen2.5:7b"
    litellm_params:
      model: "openai/Qwen2.5-7B-Instruct"
      api_base: "http://localhost:30000/v1"
      api_key: "dummy"
      max_retries: 2
      timeout: 300

  # Тяжёлая модель — сложные задачи
  - model_name: "qwen2.5:72b"
    litellm_params:
      model: "openai/Qwen2.5-72B-Instruct"
      api_base: "http://localhost:30001/v1"
      api_key: "dummy"
      timeout: 600

  # Embedding модель для RAG
  - model_name: "qwen3-embedding"
    litellm_params:
      model: "openai/qwen3-embedding-0.6b"
      api_base: "http://localhost:30002/v1"
      api_key: "dummy"

  # Fallback на облако если GPU сервер лёг
  - model_name: "qwen2.5:7b"
    litellm_params:
      model: "gpt-4o-mini"
      api_key: "sk-..."

litellm_settings:
  num_retries: 2
  request_timeout: 300
  allowed_fails: 3

general_settings:
  master_key: "sk-rugpt-master"
```

### Запуск

```bash
litellm --config litellm_config.yaml --port 4000 --host 0.0.0.0
```

### Что обеспечивает LiteLLM

1. **Маршрутизация.** `model="qwen2.5:7b"` → SGLang #1. Engine не знает про инстансы, знает только имена моделей.
2. **Load balancing.** Два инстанса одной модели → запросы распределяются (round-robin, least-busy, latency-based).
3. **Fallback.** SGLang #1 упал → retry. Все SGLang упали → fallback на OpenAI cloud.
4. **Health checks.** Пингует backends, мёртвые исключает из ротации.
5. **Logging.** Каждый запрос: модель, tokens, latency. Можно подключить Prometheus.
6. **Rate limiting.** Лимиты по API ключу / пользователю. Контроль расходов при cloud fallback.

---

## Изменения в Engine

Единственное изменение — переменная окружения:

```env
# Было (Ollama):
LLM_BASE_URL=http://zver:11434

# Стало (LiteLLM на Zver'е):
LLM_BASE_URL=http://zver:4000/v1
```

`ChatOllama` из LangChain работает с OpenAI-compatible API. SGLang и LiteLLM говорят тот же протокол. Замена прозрачная.

Опционально: заменить `ChatOllama` на `ChatOpenAI` в `AgentExecutor._create_llm()` — один import, чище семантически.

---

## Прохождение запроса по слоям

```
1. Пользователь: "@@lawyer проверь договор"

2. Engine (MentionService):
   role = lawyer
   model = role.model_name = "qwen2.5:7b"

3. AgentExecutor:
   llm = ChatOllama(base_url="http://zver:4000/v1", model="qwen2.5:7b")
   prompt = PromptCache.get_prompt(role)  # "Ты юрист компании..." (2000 токенов)
   result = await run_simple_agent(llm, prompt, messages)

4. HTTP → LiteLLM (:4000):
   POST /v1/chat/completions
   {"model": "qwen2.5:7b", "messages": [...]}
   → route to localhost:30000

5. SGLang #1 (:30000):
   a. RadixAttention: "Ты юрист компании..." → CACHE HIT
   b. Считает только новую часть: "проверь договор"
   c. Continuous batching: влетает в текущий GPU batch
   d. Генерация ответа

6. Response → LiteLLM → Engine → WebClient → пользователь
```

---

## Масштабирование

### Добавить модель

```bash
# 1. Запустить SGLang instance
python -m sglang.launch_server --model-path deepseek-v3 --port 30003 --tp 4

# 2. Добавить в litellm_config.yaml
- model_name: "deepseek-v3"
  litellm_params:
    model: "openai/deepseek-v3"
    api_base: "http://localhost:30003/v1"

# 3. Перезапустить LiteLLM (или hot reload)
```

### Горизонтальное масштабирование одной модели

Два инстанса одной модели на разных GPU — LiteLLM раскидает нагрузку:

```yaml
- model_name: "qwen2.5:7b"
  litellm_params:
    model: "openai/Qwen2.5-7B-Instruct"
    api_base: "http://localhost:30000/v1"    # GPU 0

- model_name: "qwen2.5:7b"
  litellm_params:
    model: "openai/Qwen2.5-7B-Instruct"
    api_base: "http://localhost:30010/v1"    # GPU 4
```

### Добавить второй GPU-сервер

SGLang instances на втором сервере, в LiteLLM добавить их `api_base`:

```yaml
- model_name: "qwen2.5:7b"
  litellm_params:
    model: "openai/Qwen2.5-7B-Instruct"
    api_base: "http://zver2:30000/v1"       # второй сервер
```

### Увеличить GPU на модель

Поднять `--tp` (tensor parallelism). Модель распределяется по большему числу GPU — быстрее prefill, больше VRAM под KV-кеш.

---

## Распределение GPU (примерный план)

При ~200GB VRAM (например, 4x A100 80GB или аналог):

| Instance | Модель | GPU | VRAM | Назначение |
|----------|--------|-----|------|------------|
| SGLang #1 | Qwen2.5-7B-Instruct | GPU 0 | ~20GB | Быстрые ответы, простые роли |
| SGLang #2 | Qwen2.5-72B-Instruct (fp8) | GPU 1-3 | ~150GB | Сложные задачи |
| SGLang #3 | qwen3-embedding-0.6b | GPU 0 (shared) | ~2GB | RAG embeddings |

Точное распределение зависит от конфигурации GPU на Zver'е. Embedding модель маленькая — можно разделить GPU 0 с лёгкой моделью.

---

## Мониторинг

### Health check (аналог текущего)

```bash
# SGLang instances напрямую
curl http://localhost:30000/health
curl http://localhost:30001/health

# Через LiteLLM
curl http://zver:4000/health
```

### Метрики SGLang

SGLang экспортирует метрики в Prometheus-формате:

```bash
curl http://localhost:30000/metrics
```

Ключевые метрики:
- `sglang_num_running_requests` — текущие запросы в обработке
- `sglang_num_waiting_requests` — очередь
- `sglang_cache_hit_rate` — RadixAttention cache hits
- `sglang_token_throughput` — tok/s

### LiteLLM logging

LiteLLM логирует каждый запрос: модель, токены, latency, статус. Подключаемые backends: Prometheus, Langfuse, custom callbacks.

---

## Риски и митигация

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| SGLang менее зрелый чем vLLM | Средняя | OpenAI API одинаковый. Замена SGLang→vLLM = смена launch command, конфиг LiteLLM без изменений. |
| Модель не поддерживается SGLang | Низкая | SGLang поддерживает все mainstream модели (Qwen, Llama, DeepSeek, Mistral). Day-0 support для новых. |
| LiteLLM — лишний слой, добавляет latency | Низкая | LiteLLM — тонкий HTTP proxy, overhead <5ms. Выигрыш от маршрутизации и fallback перевешивает. |
| Сложность настройки multi-GPU | Средняя | SGLang `--tp N` работает из коробки. Не требует ручной конфигурации NCCL. |

---

## Источники

- [LLM Inference Servers Compared: vLLM vs TGI vs SGLang vs Triton (2026)](https://blog.premai.io/llm-inference-servers-compared-vllm-vs-tgi-vs-sglang-vs-triton-2026/)
- [Ollama vs vLLM: Deep Dive Performance Benchmarking (Red Hat)](https://developers.redhat.com/articles/2025/08/08/ollama-vs-vllm-deep-dive-performance-benchmarking)
- [Comprehensive Comparison of LLM Inference Engines (n1n.ai)](https://explore.n1n.ai/blog/llm-inference-engine-comparison-vllm-tgi-tensorrt-sglang-2026-03-13)
- [SGLang GitHub](https://github.com/sgl-project/sglang)
- [SGLang Documentation](http://docs.sglang.io/)
- [vLLM Parallelism & Scaling Docs](https://docs.vllm.ai/en/stable/serving/parallelism_scaling/)
- [Ollama Production Limitations](https://aicompetence.org/ollama-production-limitations/)
- [SGLang vs vLLM: Which is Better (2026)](https://dev.to/kevin_0dbce07927e763d2120/sglang-vs-vllm-which-is-better-for-your-needs-in-2026-75l)
- [LiteLLM Proxy Docs](https://docs.litellm.ai/docs/simple_proxy)
- [LiteLLM Load Balancing](https://docs.litellm.ai/docs/proxy/load_balancing)
