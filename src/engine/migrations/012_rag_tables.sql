-- 012_rag_tables.sql
-- Grouped table migrations for RAG schema.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE TABLE IF NOT EXISTS user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
        -- сотрудник, которому принадлежит файл
    org_id UUID NOT NULL REFERENCES organizations(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
        -- руководитель, загрузивший файл
    storage_key VARCHAR(500) NOT NULL,
        -- ключ в хранилище: {org_id}/{user_id}/{file_id}.{ext}
        -- для local FS: путь относительно base_dir
        -- для S3: object key в бакете
    original_filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
        -- pdf | docx
    file_size BIGINT NOT NULL,
    content_hash TEXT NOT NULL,
    rag_status VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | indexing | finished | failed
    rag_error TEXT,
    indexed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Файлы пользователя (для RAG-фильтрации)
CREATE INDEX IF NOT EXISTS idx_user_files_user
    ON user_files(user_id)
    WHERE is_active = true;

-- Файлы организации
CREATE INDEX IF NOT EXISTS idx_user_files_org
    ON user_files(org_id);

-- Prevent active duplicate loads per organization
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_files_org_content_hash_active_uq
    ON user_files(org_id, content_hash)
    WHERE is_active = true;

-- Pending файлы для индексации
CREATE INDEX IF NOT EXISTS idx_user_files_pending_rag
    ON user_files(rag_status)
    WHERE rag_status IN ('pending', 'indexing');

COMMENT ON TABLE user_files IS 'File metadata. Binary data stored via StorageAdapter (local FS or S3). Key format: {org_id}/{user_id}/{file_id}.{ext}';
COMMENT ON COLUMN user_files.user_id IS 'Employee who owns the file (role accesses these)';
COMMENT ON COLUMN user_files.uploaded_by_user_id IS 'Manager who uploaded the file';
COMMENT ON COLUMN user_files.storage_key IS 'Storage key: {org_id}/{user_id}/{file_id}.{ext}. Same format for local FS and S3.';
COMMENT ON COLUMN user_files.rag_status IS 'pending | indexing | finished | failed';
COMMENT ON COLUMN user_files.file_type IS 'pdf | docx';


-- Prereqs (run once per DB)
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector

-- RAG docs table (current RuGPT RAG design)
CREATE TABLE IF NOT EXISTS rag_docs (
  doc_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  file_id uuid NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
  org_id  uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id uuid NULL     REFERENCES users(id)         ON DELETE SET NULL,

  -- Routing flag: if true, use table-row retrieval pipeline
  is_table boolean NOT NULL DEFAULT false,

  doc_title text NOT NULL DEFAULT '',
  summary   text NOT NULL DEFAULT '',

  -- Document-level embedding (typically embed: doc_title + "\n" + summary)
  summary_embedding vector(768),

  uploaded_at timestamptz NOT NULL DEFAULT now(),
  created_at  date,

  -- Full-text search over title+summary (Russian config; supports English tokens too)
  tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('russian', coalesce(doc_title, '')), 'A') ||
    setweight(to_tsvector('russian', coalesce(summary,   '')), 'B')
  ) STORED,

  CONSTRAINT rag_docs_file_id_uq UNIQUE (file_id)
);

-- Common access-filter indexes
CREATE INDEX IF NOT EXISTS rag_docs_org_id_idx
  ON rag_docs (org_id);

CREATE INDEX IF NOT EXISTS rag_docs_org_user_idx
  ON rag_docs (org_id, user_id);

-- Useful for time-range queries/sorting
CREATE INDEX IF NOT EXISTS rag_docs_org_uploaded_at_idx
  ON rag_docs (org_id, uploaded_at DESC);

-- TSV index
CREATE INDEX IF NOT EXISTS rag_docs_tsv_gin_idx
  ON rag_docs USING GIN (tsv);

-- Vector index (choose ops to match your distance operator)
-- If you use cosine distance operator <=> :
CREATE INDEX IF NOT EXISTS rag_docs_summary_embedding_hnsw_idx
  ON rag_docs USING hnsw (summary_embedding vector_cosine_ops)
  WHERE summary_embedding IS NOT NULL;


CREATE TABLE IF NOT EXISTS chunks (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  doc_id       uuid NOT NULL
               REFERENCES rag_docs(doc_id) ON DELETE CASCADE,

  chunk_text   text NOT NULL,

  embedding    vector(768) NOT NULL,

  metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,

  chunk_index  integer,

  -- Full-text search column (Russian morphology)
  tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('russian', coalesce(chunk_text, ''))
  ) STORED
);

-- Needed for doc scoping
CREATE INDEX IF NOT EXISTS chunks_doc_id_idx
  ON chunks (doc_id);

-- Vector index (semantic search)
CREATE INDEX IF NOT EXISTS chunks_emb_hnsw_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);

-- Full-text search index (lexical / phrase search)
CREATE INDEX IF NOT EXISTS chunks_tsv_gin_idx
  ON chunks USING GIN (tsv);


-- Table rows extracted from table-like chunks.
-- Each row is embedded separately for better retrieval over tables.

CREATE TABLE IF NOT EXISTS tables_rows_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Ownership / access scope comes from rag_docs via doc_id join
  doc_id uuid NOT NULL REFERENCES rag_docs(doc_id) ON DELETE CASCADE,

  -- Optional but recommended: link back to the chunk that represents the whole table
  table_chunk_id uuid NULL REFERENCES chunks(id) ON DELETE SET NULL,

  -- Positioning inside a table
  row_index integer NOT NULL,           -- 0-based or 1-based, your choice (be consistent)
  row_text text NOT NULL,

  -- Retrieval fields
  embedding vector(768) NOT NULL,

  -- Lexical search for row text (optional but very useful for tables)
  tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('russian', coalesce(row_text, ''))
  ) STORED,

  -- Extra structure you may want later: columns, header mapping, numeric values, etc.
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),

  -- Prevent duplicates inside same table chunk
  CONSTRAINT tables_rows_chunks_uq UNIQUE (doc_id, table_chunk_id, row_index)
);

-- Fast filtering by doc/table
CREATE INDEX IF NOT EXISTS tables_rows_chunks_doc_id_idx
  ON tables_rows_chunks (doc_id);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_table_chunk_id_idx
  ON tables_rows_chunks (table_chunk_id);

-- Full-text index for table row text
CREATE INDEX IF NOT EXISTS tables_rows_chunks_tsv_gin_idx
  ON tables_rows_chunks USING GIN (tsv);

-- Vector index (choose ONE depending on what you use elsewhere)
-- If you're using pgvector HNSW:
CREATE INDEX IF NOT EXISTS tables_rows_chunks_embedding_hnsw_idx
  ON tables_rows_chunks USING hnsw (embedding vector_cosine_ops);

-- If instead you use L2 distance in queries (<->), use vector_l2_ops:
-- CREATE INDEX IF NOT EXISTS tables_rows_chunks_embedding_hnsw_idx
--   ON tables_rows_chunks USING hnsw (embedding vector_l2_ops);

