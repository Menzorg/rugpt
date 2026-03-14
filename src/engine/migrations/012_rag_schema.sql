-- 012_rag_schema.sql
-- RAG schema: pgvector extension, ALTER user_files, rag_docs, chunks, tables_rows_chunks

-- Prerequisites
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- =================================================================
-- 1. ALTER user_files: add content_hash + unique index
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

-- =================================================================
-- 2. rag_docs: document-level metadata + summary embedding
-- =================================================================

CREATE TABLE IF NOT EXISTS rag_docs (
    doc_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    file_id UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
    org_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id UUID NULL     REFERENCES users(id)         ON DELETE SET NULL,

    is_table BOOLEAN NOT NULL DEFAULT false,

    doc_title TEXT NOT NULL DEFAULT '',
    summary   TEXT NOT NULL DEFAULT '',

    summary_embedding vector(1024),

    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  DATE,

    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(doc_title, '')), 'A') ||
        setweight(to_tsvector('russian', coalesce(summary,   '')), 'B')
    ) STORED,

    CONSTRAINT rag_docs_file_id_uq UNIQUE (file_id)
);

CREATE INDEX IF NOT EXISTS rag_docs_org_id_idx
    ON rag_docs (org_id);

CREATE INDEX IF NOT EXISTS rag_docs_org_user_idx
    ON rag_docs (org_id, user_id);

CREATE INDEX IF NOT EXISTS rag_docs_org_uploaded_at_idx
    ON rag_docs (org_id, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS rag_docs_tsv_gin_idx
    ON rag_docs USING GIN (tsv);

CREATE INDEX IF NOT EXISTS rag_docs_summary_embedding_hnsw_idx
    ON rag_docs USING hnsw (summary_embedding vector_cosine_ops)
    WHERE summary_embedding IS NOT NULL;

-- =================================================================
-- 3. chunks: text chunks with embeddings + full-text search
-- =================================================================

CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    doc_id UUID NOT NULL
        REFERENCES rag_docs(doc_id) ON DELETE CASCADE,

    chunk_text TEXT NOT NULL,

    embedding vector(1024) NOT NULL,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    chunk_index INTEGER,

    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(chunk_text, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS chunks_doc_id_idx
    ON chunks (doc_id);

CREATE INDEX IF NOT EXISTS chunks_emb_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS chunks_tsv_gin_idx
    ON chunks USING GIN (tsv);

-- =================================================================
-- 4. tables_rows_chunks: table row embeddings
-- =================================================================

CREATE TABLE IF NOT EXISTS tables_rows_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    doc_id UUID NOT NULL REFERENCES rag_docs(doc_id) ON DELETE CASCADE,

    table_chunk_id UUID NULL REFERENCES chunks(id) ON DELETE SET NULL,

    row_index INTEGER NOT NULL,
    row_text TEXT NOT NULL,

    embedding vector(1024) NOT NULL,

    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('russian', coalesce(row_text, ''))
    ) STORED,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT tables_rows_chunks_uq UNIQUE (doc_id, table_chunk_id, row_index)
);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_doc_id_idx
    ON tables_rows_chunks (doc_id);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_table_chunk_id_idx
    ON tables_rows_chunks (table_chunk_id);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_tsv_gin_idx
    ON tables_rows_chunks USING GIN (tsv);

CREATE INDEX IF NOT EXISTS tables_rows_chunks_embedding_hnsw_idx
    ON tables_rows_chunks USING hnsw (embedding vector_cosine_ops);
