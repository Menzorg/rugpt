-- Migration 053: trigram search on content_types.name
--              + search_related_docs overload with optional content_type filter
--              + list_categories tool added to doc_search role
--
-- search_content_types: fuzzy name lookup for the list_categories agent tool.
-- search_related_docs(…, p_content_type_id): new overload that narrows results
--   to files whose content_type_id matches (NULL = no filter, same as before).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_content_types_name_trgm
    ON content_types USING GIN (name gin_trgm_ops)
    WHERE is_active = true;

-- search_content_types(p_org_id, p_query, p_threshold)
-- Returns active content types for the org whose name is similar to p_query.
-- p_threshold: similarity threshold in [0,1]; lower = looser match (default 0.1).
CREATE OR REPLACE FUNCTION search_content_types(
    p_org_id    uuid,
    p_query     text,
    p_threshold real DEFAULT 0.1
)
RETURNS TABLE (
    id          uuid,
    org_id      uuid,
    name        text,
    description text,
    is_active   boolean,
    created_at  timestamptz,
    updated_at  timestamptz,
    similarity  real
)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.id,
        ct.org_id,
        ct.name,
        ct.description,
        ct.is_active,
        ct.created_at,
        ct.updated_at,
        similarity(ct.name, p_query) AS similarity
    FROM content_types ct
    WHERE ct.org_id = p_org_id
      AND ct.is_active = true
      AND similarity(ct.name, p_query) >= p_threshold
    ORDER BY similarity(ct.name, p_query) DESC, ct.name;
END;
$$;

COMMENT ON FUNCTION search_content_types(uuid, text, real) IS
    'Fuzzy trigram search over active content_types names for an org. '
    'p_threshold controls match looseness: lower = more results (default 0.1).';

-- New overload of search_related_docs with p_content_type_id parameter.
-- Drops all prior signatures first so the new one is the only overload.
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean);
DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text);

CREATE FUNCTION search_related_docs(
  p_org_id           uuid,
  p_user_id          uuid,
  p_query            text,
  p_query_emb        vector(1024),
  p_top_k            integer,
  p_is_admin         boolean DEFAULT false,
  p_filter_user_id   uuid    DEFAULT NULL,
  p_exclude_images   boolean DEFAULT true,
  p_search_mode      text    DEFAULT 'abstract',
  p_content_type_id  uuid    DEFAULT NULL
)
RETURNS TABLE (
  doc_id                    uuid,
  org_id                    uuid,
  user_id                   uuid,
  doc_title                 varchar(500),
  summary                   text,
  comment                   text,
  content_type_id           uuid,
  content_type_name         text,
  content_type_description  text,
  uploaded_at               timestamptz,
  created_at                date,
  vec_dist                  double precision,
  tsv_score                 real,
  mode_used                 text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_tsquery tsquery;
  v_pool    integer;
BEGIN
  v_pool := GREATEST(p_top_k * 10, 50);
  v_tsquery := plainto_tsquery('russian', p_query);

  IF p_search_mode NOT IN ('concrete', 'abstract') THEN
    RAISE EXCEPTION 'search_related_docs p_search_mode must be concrete or abstract, got %', p_search_mode;
  END IF;

  IF p_search_mode = 'concrete' THEN
    RETURN QUERY
    WITH lex AS (
      SELECT
        uf.id AS doc_id,
        ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], uf.tsv, v_tsquery, 1) AS tsv_score
      FROM user_files uf
      WHERE uf.org_id    = p_org_id
        AND (p_filter_user_id IS NULL OR uf.user_id = p_filter_user_id)
        AND (p_content_type_id IS NULL OR uf.content_type_id = p_content_type_id)
        AND (uf.is_public OR uf.user_id = p_user_id OR p_is_admin)
        AND (
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
    WHERE uf.summary_embedding IS NOT NULL AND l.tsv_score > 0
    ORDER BY l.tsv_score ASC
    LIMIT p_top_k;

  ELSE
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
        AND (p_content_type_id IS NULL OR uf.content_type_id = p_content_type_id)
        AND (uf.is_public OR uf.user_id = p_user_id OR p_is_admin)
        AND (
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

COMMENT ON FUNCTION search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text, uuid) IS
  'p_search_mode chooses concrete TSV-first or abstract vector-first search. '
  'p_is_admin allows org admins to search all active documents including private files. '
  'p_filter_user_id narrows to one file owner. '
  'p_exclude_images omits image files. '
  'p_content_type_id narrows to one content type (NULL = no filter).';

-- Add list_categories tool to the doc_search role. Idempotent.
UPDATE roles
SET tools      = tools || '["list_categories"]'::jsonb,
    updated_at = NOW()
WHERE code = 'doc_search'
  AND NOT (tools @> '["list_categories"]'::jsonb);
