# RAG-система RuGPT

Retrieval-Augmented Generation: загрузка документов, индексация, гибридный поиск (vector + full-text).

## Инфраструктурное размещение

| Компонент | Узел | Адрес |
|---|---|---|
| RAGService / IngestQueue / rag_tool | rugpt-container (B.I.1) | в uvicorn-процессе Engine |
| Apache Tika | docker-vm (B.II.2) | `http://192.168.1.84:9998` (no auth, CVE-prone — security-пункт 20) |
| LiteLLM (embeddings + summary) | Zver (C) | `http://192.168.1.80:4000/v1` (Bearer sk-dummy) |
| pgvector + chunks + tables_rows_chunks | postgres-vm (B.II.1) | `192.168.1.82:5432` |
| Binary uploads | rugpt-container | `/root/rugpt/uploads/{org_id}/{user_id}/{file_id}.{ext}` (80 GB диск) |

**Multi-tenancy & isolation:**
- pgvector — **одна общая схема**, не per-role.
- Scope в SQL-функциях: `org_id = ... AND (is_public OR user_id = viewer) AND is_active`.
- Vector dim: **1024**, индекс HNSW.
- Embeddings-модель: `hosted_vllm/Qwen/Qwen3-Embedding-0.6B` через LiteLLM.

## Архитектура

```
WebClient                    Engine (FastAPI)
   |                              |
   +-- upload file ------------>  FileService
   |                              |-- validate (тип, размер, дубликат по SHA-256)
   |                              |-- StorageAdapter.save() --> локальная ФС
   |                              |-- metadata --> PostgreSQL (user_files)
   |                              |-- IngestQueue.submit() --> фоновый поток
   |                              |
   |   (RAG indexing)             IngestQueue (ThreadPoolExecutor, 3 воркера)
   |                              |-- Tika: извлечение текста
   |                              |-- TextSplitter: нарезка на чанки (или парсинг таблицы)
   |                              |-- OpenAIEmbeddings (LiteLLM): векторизация
   |                              |-- LLM: генерация summary
   |                              |-- PostgreSQL + pgvector: сохранение
   |                              |
   |   (RAG search)               rag_search tool (LangChain @tool)
   |                              |-- Stage 1: search_related_docs() --> релевантные документы
   |                              |-- Stage 2: search_rag() --> чанки/строки внутри документа
   |                              +-- результат --> агенту в контекст
```

## Хранение файлов

Бинарные файлы хранятся на **локальной файловой системе** через `StorageAdapter`.

**Файлы:**
- `src/engine/storage/storage_adapter.py` -- `StorageAdapter` (ABC) + `LocalStorageAdapter`
- `src/engine/services/file_service.py` -- `FileService`

**Интерфейс StorageAdapter:**
```python
class StorageAdapter(ABC):
    async def save(key: str, data: bytes, content_type: str) -> None
    async def read(key: str) -> bytes
    async def delete(key: str) -> None
    async def exists(key: str) -> bool
```

**Ключ хранения:** `{org_id}/{user_id}/{file_id}.{ext}`

**FileService при загрузке:**
1. Валидация типа файла (`ALLOWED_FILE_TYPES`) и размера (`MAX_FILE_SIZE`)
2. SHA-256 хеш для детекции дубликатов в рамках пользователя
3. Определение `is_table` по расширению (`TABLE_EXTENSIONS`: xlsx, csv и т.д.)
4. Сохранение бинарных данных через `LocalStorageAdapter`
5. Создание записи `user_files` в PostgreSQL
6. Постановка в очередь индексации через `IngestQueue`

**Конфигурация (.env):**
```
STORAGE_BACKEND=local                    # пока только local
STORAGE_LOCAL_DIR=<project_root>/uploads  # директория хранения
```

## Индексация (RAGService)

**Файлы:**
- `src/engine/services/rag_service.py` -- `RAGService`
- `src/engine/storage/rag_store.py` -- `RAG_store`
- `src/engine/tasks/ingest_queue.py` -- `IngestQueue`

### Два пути индексации

**Текстовые документы** (PDF, DOCX, TXT и др.):
1. Apache Tika извлекает текст
2. `RecursiveCharacterTextSplitter` нарезает на чанки (1000 символов, overlap 200)
3. `OpenAIEmbeddings (LiteLLM)` генерирует вектор для каждого чанка (1024 dim)
4. LLM генерирует summary документа
5. Summary тоже векторизуется
6. Атомарная запись: `user_files` (summary + summary_embedding) + `chunks`

**Табличные документы** (XLSX, CSV):
1. Apache Tika парсит в XHTML
2. BeautifulSoup извлекает строки таблиц
3. Каждая строка форматируется как `"Header1: value1, Header2: value2, ..."`
4. `OpenAIEmbeddings (LiteLLM)` генерирует вектор для каждой строки
5. LLM генерирует summary (первые 50 строк + заголовки)
6. Атомарная запись: `user_files` (summary + summary_embedding) + `tables_rows_chunks`

### Жизненный цикл rag_status

```
upload --> "pending"
ingest start --> "indexing"
ingest success --> "indexed"
ingest error --> "failed"
retry --> "indexing" --> "indexed" / "failed"
delete --> "unindexed"
```

### IngestQueue

Фоновая очередь на `ThreadPoolExecutor` (3 воркера). Каждый воркер создает свой event loop и свои пулы asyncpg (нельзя переиспользовать пул из основного цикла).

```python
from ..tasks.ingest_queue import ingest_queue
ingest_queue.submit(file_id, org_id, user_id, filename, data)
```

Retry: до 3 попыток (`try_ingest`), в DEBUG-режиме без повторов.

Интерфейс спроектирован для замены на Kafka без изменения вызывающего кода.

## Схема БД

Миграции: `012_rag_schema.sql`, `013_rag_functions.sql`.

### Расширения PostgreSQL
```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector
```

### Таблица user_files (расширение миграцией 012)

RAG-поля добавленные к основной таблице файлов:

| Колонка | Тип | Описание |
|---------|-----|----------|
| content_hash | TEXT NOT NULL | SHA-256 хеш содержимого |
| summary | TEXT NOT NULL DEFAULT '' | LLM-резюме документа |
| summary_embedding | vector(1024) | Вектор summary для doc-level поиска |
| is_table | BOOLEAN DEFAULT false | Табличный документ |
| is_public | BOOLEAN DEFAULT false | Виден всем в организации |
| tsv | tsvector GENERATED | Полнотекстовый поиск (filename rank A + summary rank B) |

**Индексы:**
- `user_files_tsv_gin_idx` -- GIN по tsv (WHERE is_active)
- `user_files_summary_embedding_hnsw_idx` -- HNSW по summary_embedding (WHERE NOT NULL AND is_active)
- `idx_user_files_org_content_hash_active_uq` -- уникальность контента в организации

### Таблица chunks

Чанки текстовых документов. FK --> user_files(id) ON DELETE CASCADE.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID PK | |
| file_id | UUID FK | Ссылка на user_files |
| chunk_text | TEXT NOT NULL | Текст чанка |
| embedding | vector(1024) NOT NULL | Вектор чанка |
| metadata | JSONB DEFAULT '{}' | `{chunk_index: N}` |
| chunk_index | INTEGER | Порядковый номер в документе |
| tsv | tsvector GENERATED | Полнотекстовый поиск по chunk_text |

**Индексы:** file_id, HNSW по embedding, GIN по tsv.

### Таблица tables_rows_chunks

Строки табличных документов. FK --> user_files(id) ON DELETE CASCADE.

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | UUID PK | |
| file_id | UUID FK | Ссылка на user_files |
| table_chunk_id | UUID FK NULL | Ссылка на chunks (опционально) |
| row_index | INTEGER NOT NULL | Номер строки в таблице |
| row_text | TEXT NOT NULL | Форматированная строка |
| embedding | vector(1024) NOT NULL | Вектор строки |
| tsv | tsvector GENERATED | Полнотекстовый поиск |
| metadata | JSONB DEFAULT '{}' | `{row_index: N}` |

**Constraint:** UNIQUE (file_id, table_chunk_id, row_index).

## Гибридный поиск

Двухуровневый поиск: сначала документы, потом содержимое внутри документа.

### Stage 1: search_related_docs()

Находит релевантные документы в scope организации/пользователя.

**Вход:** org_id, user_id, query, query_embedding, top_k

**Scope:** `org_id = p_org_id AND (is_public OR user_id = p_user_id) AND is_active`

**Алгоритм:**
1. Проверяет наличие TSV-совпадений в user_files
2. **Если есть TSV-хиты (concrete mode):** TSV-first -- сначала фильтрует по полнотекстовому совпадению, затем ранжирует по векторной близости summary_embedding
3. **Если нет TSV-хитов (abstract mode):** Vector-first -- сначала ранжирует по косинусной близости summary_embedding (отсечка < 0.5), затем дообогащает TSV-скором. Формула: `r_vec + 0.3 * r_tsv`

**Возвращает:** doc_id, doc_title, summary, vec_dist, tsv_score, mode_used

### Stage 2: search_rag()

Диспетчер: определяет тип документа (is_table) и вызывает нужную функцию.

**Для текстовых документов:**
- `search_concrete_chunks()` -- TSV-first: сначала полнотекстовый фильтр, потом rank fusion. Формула: `r_vec + tsv_weight * r_tsv`
- `search_abstract_chunks()` -- Vector-first: сначала по векторной близости, потом дообогащение TSV. Формула: `r_vec + 0.3 * r_tsv`

**Для табличных документов:**
- `search_concrete_table_rows()` -- аналогично concrete_chunks, но по tables_rows_chunks
- `search_abstract_table_rows()` -- аналогично abstract_chunks, но по tables_rows_chunks

**Fallback в rag_tool:** если выбранный метод не дал результатов, пробует альтернативный (concrete <-> abstract).

### get_expanded_context()

Расширенный контекст: находит лучший чанк через search_concrete_chunks, затем возвращает окно из соседних чанков (chunk_index +/- distance).

## RAG Tool (агентный инструмент)

**Файл:** `src/engine/agents/tools/rag_tool.py`

LangChain `@tool` с двухуровневым поиском. org_id и user_id инжектируются через `RunnableConfig` -- LLM видит только параметр `query`.

```python
@tool(response_format="content")
async def rag_search(query: str, config: RunnableConfig) -> str:
    # 1. search_related_docs() -> top-3 документа
    # 2. search_rag() -> top-3 чанка/строки из каждого документа
    # 3. Форматирование: "## Title\n[chunk] text\n..."
```

**Инициализация:** `init_rag_pool(pool)` при старте Engine устанавливает shared asyncpg pool.

## API эндпоинты

**Роутер:** `src/engine/routes/rag.py`, prefix `/api/v1/rag`

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/docs/ingest` | Индексировать загруженный файл |
| DELETE | `/docs/{file_id}` | Удалить документ из индекса |
| POST | `/docs/{file_id}/retry` | Повторить индексацию (через IngestQueue) |
| GET | `/docs/find` | Найти релевантные документы (`?query=&top_k=5`) |
| GET | `/docs/{file_id}/search/abstract` | Vector-first поиск внутри документа |
| GET | `/docs/{file_id}/search/concrete` | TSV-first поиск внутри документа (`?tsv_weight=1.0`) |

Все эндпоинты требуют JWT-авторизацию.

## Модели данных

**Файл:** `src/engine/models/rag.py`

```python
@dataclass
class RelatedDoc:
    """Результат doc-level поиска (search_related_docs)"""
    file_id: str
    org_id: str
    user_id: Optional[str]
    doc_title: str
    summary: str
    uploaded_at: Optional[datetime]
    created_at: Optional[date]
    vec_dist: float
    tsv_score: float
    mode_used: str  # 'concrete' | 'abstract'

@dataclass
class ChunkSearchResult:
    """Результат chunk-level поиска (search_rag)"""
    chunk_id: str
    file_id: str
    chunk_text: str
    vec_dist: float
    tsv_score: float
    r_vec: Optional[int]
    r_tsv: Optional[int]
    final_rank: Optional[float]
    source_type: str  # 'chunk' | 'table_row'
```

## Конфигурация (.env)

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| EMBEDDING_MODEL | hosted_vllm/Qwen/Qwen3-Embedding-0.6B | Модель эмбеддингов (LiteLLM alias) |
| RAG_SUMMARY_MODEL | (DEFAULT_MODEL) | Модель для генерации summary |
| RAG_TIKA_SERVER_ENDPOINT | http://192.168.1.84:9998 | Apache Tika сервер (docker-vm B.II.2) |
| RAG_STORE_DSN | (POSTGRES_DSN) | DSN для RAG-хранилища |
| RAG_VECTOR_DIM | 1024 | Размерность векторов |
| RAG_CHUNK_SIZE | 1000 | Размер чанка (символы) |
| RAG_CHUNK_OVERLAP | 200 | Перекрытие чанков |
| RAG_SUMMARY_INPUT_MAX_CHARS | 8000 | Макс. текста для summary |

## Зависимости

```
langchain-openai     # OpenAIEmbeddings, ChatOpenAI (работают с LiteLLM)
langchain            # RecursiveCharacterTextSplitter
tika                 # Apache Tika клиент (парсинг документов)
beautifulsoup4       # Парсинг таблиц из XHTML
asyncpg              # PostgreSQL (pgvector)
aiofiles             # Асинхронное чтение/запись файлов
```

## SQL-функции (миграция 013)

| Функция | Назначение |
|---------|-----------|
| search_concrete_chunks | TSV-first гибридный поиск по чанкам |
| search_abstract_chunks | Vector-first гибридный поиск по чанкам |
| search_concrete_table_rows | TSV-first гибридный поиск по строкам таблиц |
| search_abstract_table_rows | Vector-first гибридный поиск по строкам таблиц |
| search_related_docs | Doc-level поиск по user_files (summary + tsv) |
| search_rag | Диспетчер: определяет is_table и вызывает нужную функцию |
| get_expanded_context | Расширенный контекст: окно соседних чанков |
