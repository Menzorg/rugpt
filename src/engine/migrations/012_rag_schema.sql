-- 012_rag_schema.sql
-- RAG schema: pgvector, user_files (primary), chunks, tables_rows_chunks
--
-- Design: user_files is the primary document table.
-- summary, summary_embedding, is_table and tsv live here so all doc-level
-- search runs directly against user_files.
-- chunks and tables_rows_chunks reference user_files(id) via file_id.

-- Prerequisites
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- =================================================================
-- 1. ALTER user_files: content_hash + RAG fields
-- =================================================================

ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS content_hash TEXT;

UPDATE user_files SET content_hash = '' WHERE content_hash IS NULL;

ALTER TABLE user_files
    ALTER COLUMN content_hash SET NOT NULL;

-- Prevent active duplicate loads per organization
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_files_org_content_hash_active_uq
    ON user_files(org_id, content_hash)
    WHERE is_active = true AND content_hash != '';

-- Document summary produced by LLM during ingestion
ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';

-- Vector embedding of the summary (1024-dim); NULL until indexed
ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS summary_embedding vector(1024);

-- True when the file was parsed as a structured table
ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS is_table BOOLEAN NOT NULL DEFAULT false;

-- Full-text search vector: filename ranked A (most specific),
-- summary ranked B (broader context)
ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('russian', coalesce(original_filename, '')), 'A') ||
            setweight(to_tsvector('russian', coalesce(summary,            '')), 'B')
        ) STORED;

CREATE INDEX IF NOT EXISTS user_files_tsv_gin_idx
    ON user_files USING GIN (tsv)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS user_files_summary_embedding_hnsw_idx
    ON user_files USING hnsw (summary_embedding vector_cosine_ops)
    WHERE summary_embedding IS NOT NULL AND is_active = true;

-- =================================================================
-- 2. chunks: text chunks with embeddings
--    FK → user_files(id) directly (no rag_docs indirection).
-- =================================================================

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    file_id UUID NOT NULL
        REFERENCES user_files(id) ON DELETE CASCADE,

    chunk_text  TEXT         NOT NULL,
    embedding   vector(1024) NOT NULL,
    metadata    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    chunk_index INTEGER,

    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(chunk_text, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS chunks_file_id_idx
    ON chunks (file_id);

CREATE INDEX IF NOT EXISTS chunks_emb_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS chunks_tsv_gin_idx
    ON chunks USING GIN (tsv);

-- =================================================================
-- 3. tables_rows_chunks: per-row embeddings for table files
--    FK → user_files(id) directly.
-- =================================================================

CREATE TABLE IF NOT EXISTS tables_rows_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    file_id UUID NOT NULL
        REFERENCES user_files(id) ON DELETE CASCADE,

    table_chunk_id UUID NULL REFERENCES chunks(id) ON DELETE SET NULL,

    row_index INTEGER NOT NULL,
    row_text  TEXT    NOT NULL,

    embedding vector(1024) NOT NULL,

    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(row_text, ''))
    ) STORED,

    metadata   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tables_rows_chunks_uq UNIQUE (file_id, table_chunk_id, row_index)
);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_file_id_idx
    ON tables_rows_chunks (file_id);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_table_chunk_id_idx
    ON tables_rows_chunks (table_chunk_id);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_tsv_gin_idx
    ON tables_rows_chunks USING GIN (tsv);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_embedding_hnsw_idx
    ON tables_rows_chunks USING hnsw (embedding vector_cosine_ops);
