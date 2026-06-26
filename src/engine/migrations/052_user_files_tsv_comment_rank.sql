-- Migration 052: include user_files.comment in document search TSV ranking.
--
-- Weight mapping for ts_rank is ordered D, C, B, A:
--   original_filename -> A = 1.0
--   comment           -> B = 0.6
--   summary           -> C = 0.3
--   D                 -> 0.1

DROP INDEX IF EXISTS user_files_tsv_gin_idx;

ALTER TABLE user_files
    DROP COLUMN IF EXISTS tsv;

ALTER TABLE user_files
    ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('russian', coalesce(original_filename, '')), 'A') ||
            setweight(to_tsvector('russian', coalesce(comment,           '')), 'B') ||
            setweight(to_tsvector('russian', coalesce(summary,           '')), 'C')
        ) STORED;

CREATE INDEX IF NOT EXISTS user_files_tsv_gin_idx
    ON user_files USING GIN (tsv)
    WHERE is_active = true;
