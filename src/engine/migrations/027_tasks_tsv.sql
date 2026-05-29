-- Migration 027: full-text search column for tasks
-- tsvector: title at weight A (highest priority), description at weight C.
-- The GIN index makes ts_rank_cd queries fast even on large tables.

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('russian', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('russian', coalesce(description, '')), 'C')
        ) STORED;

CREATE INDEX IF NOT EXISTS tasks_tsv_gin ON tasks USING gin(tsv);
