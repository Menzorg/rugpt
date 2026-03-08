MinIO для RuGPT — анализ

  Что это

  Self-hosted S3-совместимое хранилище. Один бинарник, минимальные ресурсы, API идентичный AWS S3.

  Как встраивается в RuGPT

  WebClient                    Engine (FastAPI)                MinIO (:9000)
     │                              │                              │
     ├── upload file ──────────►    │                              │
     │                              ├── put_object() ─────────►   │
     │                              │   bucket: rugpt-files        │
     │                              │   key: {org_id}/{user_id}/   │
     │                              │        {file_id}.pdf         │
     │                              │                              │
     │                              ├── metadata → PostgreSQL      │
     │                              │                              │
     │   (RAG indexing)             │                              │
     │                              ├── get_object() ◄────────    │
     │                              │   → parse → vectorize        │
     │                              │                              │
     │◄── download (presigned) ─────┤── presigned_url() ──────►   │
     │                              │   (временная ссылка)         │

  Деплой

  MinIO ставится одной командой рядом с Engine на том же сервере (10.0.0.2):

  # Установка
  wget https://dl.min.io/server/minio/release/linux-amd64/minio
  chmod +x minio

  # Запуск (systemd или напрямую)
  MINIO_ROOT_USER=rugpt_admin \
  MINIO_ROOT_PASSWORD=secure_password \
  ./minio server /var/lib/minio/data --console-address ":9001"

  - :9000 — S3 API
  - :9001 — веб-консоль (управление бакетами, просмотр файлов)
  - Данные на диске в /var/lib/minio/data

  Python-интеграция

  Два варианта SDK:

  1. minio (официальный, синхронный)
  from minio import Minio

  client = Minio("localhost:9000", access_key="...", secret_key="...", secure=False)
  client.put_object("rugpt-files", object_name, data, length)
  client.get_object("rugpt-files", object_name)

  2. miniopy-async (асинхронный, для FastAPI)
  from miniopy_async import Minio

  client = Minio("localhost:9000", access_key="...", secret_key="...", secure=False)
  await client.put_object("rugpt-files", object_name, data, length)
  await client.get_object("rugpt-files", object_name)

  Для FastAPI лучше miniopy-async — не блокирует event loop.

  Структура бакетов

  rugpt-files/
  ├── {org_id}/
  │   ├── {user_id}/
  │   │   ├── {file_id}.pdf
  │   │   ├── {file_id}.docx
  │   │   └── ...
  │   └── {user_id}/
  │       └── ...

  Что это даёт конкретно

  ┌─────────────────────┬────────────────────────────────────────────────────────────────────────────────────────────────┐
  │     Возможность     │                                             Зачем                                              │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Presigned URLs      │ Скачивание файлов без прокси через Engine — MinIO отдаёт напрямую                              │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Versioning          │ Если руководитель перезагружает файл — старая версия сохраняется                               │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Веб-консоль (:9001) │ Админ может просматривать файлы без кода                                                       │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ S3 API              │ Если RAG-специалист использует LangChain document loaders — они умеют работать с S3 из коробки │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Lifecycle rules     │ Автоочистка старых/удалённых файлов через N дней                                               │
  ├─────────────────────┼────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ Миграция в облако   │ Потом если нужно — просто меняем endpoint на AWS/Yandex Object Storage                         │
  └─────────────────────┴────────────────────────────────────────────────────────────────────────────────────────────────┘

  Изменения в FileService

  class FileService:
      def __init__(self, file_storage, minio_client: Minio,
                   bucket: str = "rugpt-files"):

      async def upload_file(user_id, org_id, uploaded_by_user_id,
                            file_content: bytes, original_filename: str) -> UserFile:
          file_id = uuid4()
          ext = get_extension(original_filename)
          object_name = f"{org_id}/{user_id}/{file_id}.{ext}"

          # Загрузка в MinIO
          await self.minio_client.put_object(
              self.bucket, object_name,
              BytesIO(file_content), len(file_content),
              content_type=get_content_type(ext)
          )

          # Метаданные в PostgreSQL
          user_file = UserFile(
              id=file_id, user_id=user_id, org_id=org_id,
              file_path=object_name,  # путь в MinIO, не на диске
              ...
          )
          return await self.file_storage.create(user_file)

      async def get_download_url(file_id, expires=3600) -> str:
          file = await self.file_storage.get_by_id(file_id)
          return await self.minio_client.presigned_get_object(
              self.bucket, file.file_path, expires=timedelta(seconds=expires)
          )

      async def get_file_content(file_id) -> bytes:
          """Для RAG-индексации — получить содержимое файла"""
          file = await self.file_storage.get_by_id(file_id)
          response = await self.minio_client.get_object(self.bucket, file.file_path)
          return await response.read()

  Конфигурация (.env)

  MINIO_ENDPOINT=localhost:9000
  MINIO_ACCESS_KEY=rugpt_admin
  MINIO_SECRET_KEY=secure_password
  MINIO_SECURE=false
  MINIO_BUCKET=rugpt-files

  Ресурсы

  MinIO потребляет минимально:
  - RAM: ~100-200 МБ
  - CPU: почти ноль в idle
  - Диск: ровно столько, сколько весят файлы

  Зависимости

  pip install miniopy-async

  Одна библиотека, без тяжёлых зависимостей.

  ---
  Итог

  Для RuGPT MinIO имеет смысл потому что:
  1. RAG-инструменты (LangChain) нативно работают с S3
  2. Presigned URLs разгружают Engine от проксирования скачивания
  3. Веб-консоль — бонус для админа
  4. Если потом переехать в облако — меняется только endpoint в .env
  5. Ресурсов ест минимум, ставится за 5 минут

❯ "Если RAG-специалист использует LangChain document loaders — они умеют работать с S3 из коробки " ты сказал. что за ленгчейн докумнет            
  лоадерс?                                                                                                                                       
  ⎿  ⧉ Selected 1 lines from rugpt/docs/alpha-migration-plan.md in Visual Studio Code                                                              
                                                                                                                                                 
● LangChain Document Loaders — это модуль для загрузки и парсинга документов из разных источников в формат, пригодный для RAG.                     
                                                                                                                                                   
  Что делают                                                                                                                                       
                                                                                                                                                   
  Берут файл (PDF, DOCX, HTML, CSV...) → парсят содержимое → возвращают список Document объектов с текстом и метаданными. Эти документы потом      
  нарезаются на чанки и индексируются в vector store.                                                                                            
                                                                                                                                                 
  S3-совместимые лоадеры

  from langchain_community.document_loaders import S3FileLoader, S3DirectoryLoader

  # Загрузить один файл из MinIO
  loader = S3FileLoader(
      bucket="rugpt-files",
      key="org_id/user_id/file_id.pdf",
      endpoint_url="http://localhost:9000",  # MinIO endpoint
      aws_access_key_id="rugpt_admin",
      aws_secret_access_key="password",
  )
  docs = loader.load()  # → [Document(page_content="...", metadata={...})]

  # Загрузить все файлы пользователя
  loader = S3DirectoryLoader(
      bucket="rugpt-files",
      prefix="org_id/user_id/",          # все файлы этого юзера
      endpoint_url="http://localhost:9000",
  )
  docs = loader.load()  # → [Document, Document, ...]

  Какие форматы парсят

  Под капотом лоадеры используют парсеры:
  - PDF → PyPDFLoader, PDFMinerLoader, UnstructuredPDFLoader
  - DOCX → Docx2txtLoader, UnstructuredWordDocumentLoader
  - S3-лоадер автоматически определяет тип по расширению и вызывает нужный парсер

  Как это вписывается в RuGPT

  Руководитель загружает файл
       │
       ▼
  FileService → MinIO (put_object)
       │
       ▼
  RAG-индексатор (будущее):
       │
       ├── S3FileLoader(bucket, key, endpoint_url)
       │       → Document(page_content="текст документа")
       │
       ├── TextSplitter → нарезка на чанки
       │
       └── VectorStore.add_documents(chunks, metadata={file_id, user_id})

  Именно поэтому MinIO удобен — RAG-специалисту не нужно писать свой код для чтения файлов из хранилища, LangChain уже умеет ходить в S3 API
  напрямую.

  Пакет: langchain-community (уже в зависимостях проекта).

❯ А если планируется использоваться графовая веткорка это накладывает ограничения или какието еще шутки?                                           
                                                                                                         
∴ Thinking…                                                                                                                                        
                                                                                                                                                 
  The user is asking about graph-based vector databases and whether they impose any constraints or considerations on the architecture. Let me think
   about what "графовая векторка" means - it's a graph-based vector store/database, like Neo4j Vector, Apache AGE, or similar solutions that     
  combine graph structures with vector embeddings.                                                                                                 
                                                                                                                                                   
  Let me think about the main graph vector databases and how they differ from traditional vector stores, and what implications this has for the    
  architecture.

  Common graph vector databases:
  - Neo4j Vector Index (Neo4j has built-in vector search since 5.11+)
  - Apache AGE (PostgreSQL extension for graph)

  - FalkorDB
  - Amazon Neptune Analytics

  Traditional vector stores like ChromaDB, Milvus, FAISS, Qdrant, Weaviate, and pgvector handle embeddings differently, but the core question
  remains: does using a graph vector database change anything about the MinIO/file storage architecture?

  The short answer is no - the fundamental flow stays the same. Files live in MinIO, document loaders pull them out, text gets chunked, and those
  chunks get embedded and stored in whatever vector database I'm using. But graph vector databases do introduce some additional considerations
  around how I structure the relationships between entities and how I query across both the graph topology and vector similarity.

  Since they're working within the LangChain ecosystem, I should focus on what's actually supported there - Neo4j Vector, FalkorDB, and Neptune
  Analytics are the main options. The real takeaway is that switching to a graph-based approach doesn't touch the MinIO layer or file storage at
  all. It only changes how chunks are indexed with relationship edges, how queries traverse the graph alongside vector matching, what
  infrastructure needs to run, and the query patterns themselves. The user_files table and FileService remain completely unchanged.

● На хранение файлов (MinIO) и таблицу user_files — не влияет вообще. Файловое хранилище и векторное хранилище — разные слои:

  MinIO (бинарные файлы)                                                                                                                           
    │
    ▼                                                                                                                                              
  Document Loader → парсинг текста                                                                                                               
    │                                                                                                                                            
    ▼
  Text Splitter → чанки
    │
    ▼
  Embedding Model → векторы
    │
    ▼
  Vector Store (вот тут разница)
    ├── Плоская векторка: ChromaDB, Qdrant, pgvector, FAISS
    └── Графовая векторка: Neo4j Vector, FalkorDB, Neptune

  Чем графовая отличается от плоской

  Плоская (ChromaDB, pgvector): чанки лежат как отдельные записи с вектором. Поиск — чистый cosine similarity. Чанк ничего не знает о соседних
  чанках.

  Графовая (Neo4j Vector): чанки — это узлы графа, между ними есть рёбра (связи). Можно моделировать:
  - Чанк A является частью Документа X
  - Документ X относится к теме "Договоры"
  - Чанк A ссылается на Чанк B (следующий параграф)
  - Документ X загружен для Пользователя Y

  Поиск: vector similarity + обход графа. Находим похожий чанк → достаём связанные чанки → получаем больше контекста.

  Что это даёт для RuGPT

  Для вашего кейса (роли с изолированными документами) графовая векторка полезна тем, что:

  1. Изоляция по пользователю — не через фильтр file_id IN (...), а через граф: User → has_files → File → has_chunks → Chunk. Чище и быстрее на
  больших объёмах.
  2. Контекст между чанками — если юрист спрашивает про пункт 5.3 договора, графовая векторка может вернуть не только этот чанк, но и связанные
  (пункт 5.2, определения терминов из раздела 1).
  3. Связи между документами — "этот договор ссылается на тот регламент" — такие связи в плоской векторке не выразить.

  Что это накладывает на архитектуру

  ┌──────────────────────┬───────────────────────────────────────────────────────────────────────┐
  │        Аспект        │                                Влияние                                │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ MinIO / user_files   │ Никакого. Файлы хранятся так же                                       │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ FileService          │ Никакого. API тот же                                                  │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ Инфраструктура       │ Нужен Neo4j (или аналог) как отдельный сервис                         │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ RAM/CPU              │ Neo4j тяжелее чем ChromaDB/pgvector                                   │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ LangChain интеграция │ langchain-neo4j — поддерживается, но API чуть другой                  │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ Индексация           │ Сложнее — нужно создавать узлы И рёбра, а не просто вставлять векторы │
  ├──────────────────────┼───────────────────────────────────────────────────────────────────────┤
  │ Запросы              │ Мощнее, но сложнее писать (Cypher + vector)                           │
  └──────────────────────┴───────────────────────────────────────────────────────────────────────┘

  Рекомендация

  Решение за RAG-специалистом, но архитектурно от нас ничего не зависит — FileService отдаёт файлы через get_file_content(), а что с ними делает
  индексатор (плоский или графовый) — это его зона. Интерфейс user_files + MinIO работает одинаково с любым вариантом.

  Единственное что стоит заложить: в .env переменные для vector store (endpoint, credentials) — чтобы при выборе конкретного решения не менять код
  конфигурации.

✻ Cooked for 38s

❯ А то что у нас ленгграф используется а не ленгчейн?                                                                                              
                                                      
● Это не конфликт. LangGraph и LangChain — не альтернативы, они работают вместе:                                                                   
                                                                                                                                                 
  langchain-core          ← базовые абстракции (LLM, tools, prompts)                                                                               
      │                                                                                                                                          
      ├── langgraph       ← графы с состоянием (оркестрация агентов)                                                                               
      │                                                                                                                                            
      ├── langchain       ← chains, agents, RAG-утилиты                                                                                            
      │
      └── langchain-community  ← интеграции (vector stores, document loaders)

  LangGraph зависит от langchain-core, а document loaders и vector stores живут в langchain-community. Они не пересекаются — каждый делает своё.

  Как это выглядит в RuGPT

  LangGraph — оркестрация агентов (уже используется):
  # AgentExecutor → graphs/simple.py, chain.py, multi_agent.py
  # StateGraph: handle_message → build_prompt → call_llm → parse → ...

  LangChain Community — RAG-пайплайн (будет добавлен):
  # Индексация:
  from langchain_community.document_loaders import S3FileLoader
  from langchain.text_splitter import RecursiveCharacterTextSplitter
  from langchain_community.vectorstores import Neo4jVector  # или Chroma, pgvector

  # Поиск:
  retriever = vector_store.as_retriever(filter={"user_id": "..."})
  docs = await retriever.ainvoke("текст запроса")

  Точка соединения — tool rag_search внутри LangGraph-графа:

  # Граф (LangGraph) вызывает tool → tool использует retriever (LangChain)
  #
  # StateGraph node: call_llm
  #   → LLM решает вызвать tool "rag_search"
  #     → rag_search использует LangChain retriever
  #       → retriever ходит в vector store
  #     → результат возвращается в граф
  #   → LLM формирует ответ с учётом найденных документов

  Коротко

  LangGraph = мозг агента (решает что делать, в каком порядке).
  LangChain Community = руки (document loaders, vector stores, retrievers).

  Граф вызывает tool, tool внутри использует LangChain-компоненты. Конфликта нет, так и задумано.

  В requirements.txt уже стоят оба:
  langgraph
  langchain
  langchain-community
  langchain-ollama
