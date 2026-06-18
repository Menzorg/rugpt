-- Migration 052: include user_files.comment in document search TSV ranking.
--
-- Weight mapping for ts_rank is ordered D, C, B, A:
--   original_filename -> A = 1.0
--   comment           -> B = 0.6
--   summary           -> C = 0.3
--   D                 -> 0.1

DROP INDEX IF EXISTS user_files_tsv_gin_idx;

DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text);

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

CREATE FUNCTION search_related_docs(
  p_org_id uuid,
  p_user_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_is_admin boolean DEFAULT false,
  p_filter_user_id uuid DEFAULT NULL,
  p_exclude_images boolean DEFAULT true,
  p_search_mode text DEFAULT 'abstract'
)
RETURNS TABLE (
  doc_id uuid,
  org_id uuid,
  user_id uuid,
  doc_title varchar(500),
  summary text,
  comment text,
  content_type_id uuid,
  content_type_name TEXT,
  content_type_description text,
  uploaded_at timestamptz,
  created_at date,
  vec_dist double precision,
  tsv_score real,
  mode_used text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_tsquery tsquery;
  v_pool integer;
BEGIN
  v_pool := GREATEST(p_top_k * 10, 50);
  v_tsquery := plainto_tsquery('russian', p_query);

  IF p_search_mode NOT IN ('concrete', 'abstract') THEN
    RAISE EXCEPTION 'search_related_docs p_search_mode must be concrete or abstract, got %', p_search_mode;
  END IF;

  IF p_search_mode = 'concrete' THEN
    -- TSV-first: rank by text match, re-sort by vector distance
    RETURN QUERY
    WITH lex AS (
      SELECT
        uf.id AS doc_id,
        ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], uf.tsv, v_tsquery, 1) AS tsv_score
      FROM user_files uf
      WHERE uf.org_id    = p_org_id
        AND (p_filter_user_id IS NULL OR uf.user_id = p_filter_user_id)
        -- Admins may search every active document in their organization, including private files.
        AND (uf.is_public OR uf.user_id = p_user_id OR p_is_admin)
        AND (
          -- Filtering out images. That block isn't applied to filetypes outside of array
          NOT p_exclude_images
          OR (
            lower(COALESCE(uf.file_type, '')) NOT IN ('jpg', 'jpeg', 'png', 'gif', 'webp')
            AND lower(COALESCE(uf.original_filename, '')) NOT LIKE ALL(ARRAY['%.jpg', '%.jpeg', '%.png', '%.gif', '%.webp'])
          )
        )
        AND uf.is_active = true
        AND uf.tsv @@ v_tsquery
      ORDER BY tsv_score DESC
      LIMIT v_pool
    )
    SELECT
      uf.id                                  AS doc_id,
      uf.org_id                              AS org_id,
      uf.user_id                             AS user_id,
      uf.original_filename                   AS doc_title,
      uf.summary                             AS summary,
      uf.comment                             AS comment,
      uf.content_type_id                     AS content_type_id,
      ct.name                                AS content_type_name,
      ct.description                         AS content_type_description,
      uf.created_at                          AS uploaded_at,
      uf.created_at::date                    AS created_at,
      (uf.summary_embedding <=> p_query_emb) AS vec_dist,
      l.tsv_score                            AS tsv_score,
      'concrete'::text                       AS mode_used
    FROM lex l
    JOIN user_files uf ON uf.id = l.doc_id
    LEFT JOIN content_types ct ON ct.id = uf.content_type_id
    WHERE uf.summary_embedding IS NOT NULL AND tsv_score > 0
    ORDER BY tsv_score ASC
    LIMIT p_top_k;

  ELSE
    -- Vector-first: rank by embedding distance, re-score with TSV
    RETURN QUERY
    WITH vec AS (
      SELECT
        uf.id        AS doc_id,
        uf.org_id    AS org_id,
        uf.user_id   AS user_id,
        uf.summary   AS summary,
        (uf.summary_embedding <=> p_query_emb) AS vec_dist
      FROM user_files uf
      WHERE uf.org_id    = p_org_id
        AND (p_filter_user_id IS NULL OR uf.user_id = p_filter_user_id)
        -- Admins may search every active document in their organization, including private files.
        AND (uf.is_public OR uf.user_id = p_user_id OR p_is_admin)
        AND (
          -- Filtering out images. That block isn't applied to filetypes outside of array
          NOT p_exclude_images
          OR (
            lower(COALESCE(uf.file_type, '')) NOT IN ('jpg', 'jpeg', 'png', 'gif', 'webp')
            AND lower(COALESCE(uf.original_filename, '')) NOT LIKE ALL(ARRAY['%.jpg', '%.jpeg', '%.png', '%.gif', '%.webp'])
          )
        )
        AND uf.is_active = true
        AND uf.summary_embedding IS NOT NULL
      ORDER BY uf.summary_embedding <=> p_query_emb
      LIMIT v_pool
    ),
    scored AS (
      SELECT
        v.*,
        CASE
          WHEN uf.tsv @@ v_tsquery THEN ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], uf.tsv, v_tsquery)
          ELSE 0
        END AS tsv_score
      FROM vec v
      JOIN user_files uf ON uf.id = v.doc_id
    ),
    ranked AS (
      SELECT
        s.*,
        dense_rank() OVER (ORDER BY s.vec_dist ASC)   AS r_vec,
        dense_rank() OVER (ORDER BY s.tsv_score DESC) AS r_tsv
      FROM scored s
    )
    SELECT
      r.doc_id                AS doc_id,
      r.org_id                AS org_id,
      r.user_id               AS user_id,
      uf.original_filename    AS doc_title,
      r.summary               AS summary,
      uf.comment              AS comment,
      uf.content_type_id      AS content_type_id,
      ct.name                 AS content_type_name,
      ct.description          AS content_type_description,
      uf.created_at           AS uploaded_at,
      uf.created_at::date     AS created_at,
      r.vec_dist              AS vec_dist,
      r.tsv_score             AS tsv_score,
      'abstract'::text        AS mode_used
    FROM ranked r
    JOIN user_files uf ON uf.id = r.doc_id
    LEFT JOIN content_types ct ON ct.id = uf.content_type_id
    WHERE r.vec_dist < 0.65
    ORDER BY (r.r_vec + 0.3 * r.r_tsv) ASC
    LIMIT p_top_k;
  END IF;
END;
$$;

COMMENT ON FUNCTION search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text) IS
  'p_search_mode chooses concrete TSV-first search or abstract vector-first search. p_is_admin allows organization admins to search all active documents in their organization, including private files. p_filter_user_id optionally narrows results to one file owner. p_exclude_images omits image files from document search by default.';
