-- Migration 058: split filename words in TSV for concrete document search.
--
-- Problem: the TSV stored the full filename as a single underscore-joined token
-- (e.g. 'report_alenka_08-06-2026_to_08-06-2026.xlsx') so plainto_tsquery('russian', 'report')
-- could not match it — only an exact compound-lexeme hit would work.
--
-- Fix: pre-process original_filename with regexp_replace to replace every character
-- that is not a Latin letter, Cyrillic letter, or digit with a space before feeding
-- to to_tsvector. Pattern: [^A-Za-z0-9А-ЯЁа-яё]
-- Result: 'report_Alenka_08-06-2026.xlsx' -> 'report Alenka 08 06 2026 xlsx',
-- each word becomes its own TSV lexeme at weight A.
--
-- Weights are unchanged from migration 053:
--   original_filename words -> A  (strongest lexical signal)
--   comment                 -> B  (user/category-provided business context)
--   summary                 -> C  (broader generated context)

CREATE OR REPLACE FUNCTION normalize_doc_search_text(p_text text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(COALESCE(p_text, ''), '[^A-Za-z0-9А-ЯЁа-яё]', ' ', 'g');
$$;

COMMENT ON FUNCTION normalize_doc_search_text(text) IS
    'Normalizes document-search text by replacing non-letter/digit separators with spaces.';

DROP INDEX IF EXISTS user_files_tsv_gin_idx;

ALTER TABLE user_files
    DROP COLUMN IF EXISTS tsv;

ALTER TABLE user_files
    ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('russian', normalize_doc_search_text(original_filename)), 'A') ||
            setweight(to_tsvector('russian', coalesce(comment, '')), 'B') ||
            setweight(to_tsvector('russian', coalesce(summary, '')), 'C')
        ) STORED;

CREATE INDEX IF NOT EXISTS user_files_tsv_gin_idx
    ON user_files USING GIN (tsv)
    WHERE is_active = true;
