-- Migration 057: normalize document-search tsquery and fix context boosting.
--
-- Migration 055 split original_filename on punctuation before building user_files.tsv
-- so filenames like "Drugs list.xlsx" are indexed as separate lexemes:
-- "Drugs list xlsx". search_related_docs still built plainto_tsquery from the
-- raw query, where "list.xlsx" can remain a single token and fail to match.
--
-- Recreate search_related_docs with the same punctuation normalization applied
-- to p_query before building v_tsquery. Category distances are cached raw; the
-- 1 - distance context score is computed only in the ranking queries.

DROP FUNCTION IF EXISTS search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text, uuid);

CREATE FUNCTION search_related_docs(
  p_org_id                 uuid,
  p_user_id                uuid,
  p_query                  text,
  p_query_emb              vector(1024),
  p_top_k                  integer DEFAULT 5,
  p_is_admin               boolean DEFAULT false,
  p_filter_user_id         uuid DEFAULT NULL,
  p_exclude_images         boolean DEFAULT true,
  p_search_mode            text DEFAULT 'abstract',
  p_content_type_id        uuid DEFAULT NULL
)
RETURNS TABLE (
  doc_id                   uuid,
  org_id                   uuid,
  user_id                  uuid,
  doc_title                varchar(500),
  summary                  text,
  comment                  text,
  content_type_id          uuid,
  content_type_name        text,
  content_type_description text,
  uploaded_at              timestamptz,
  created_at               date,
  vec_dist                 double precision,
  tsv_score                real,
  mode_used                text,
  rank_score               double precision
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_tsquery tsquery;
  v_pool    integer;
BEGIN
  v_pool := GREATEST(p_top_k * 10, 50);
  v_tsquery := plainto_tsquery(
    'russian',
    regexp_replace(COALESCE(p_query, ''), '[^A-Za-z0-9А-ЯЁа-яё]', ' ', 'g')
  );

  IF p_search_mode NOT IN ('concrete', 'abstract') THEN
    RAISE EXCEPTION 'search_related_docs p_search_mode must be concrete or abstract, got %', p_search_mode;
  END IF;

  IF p_search_mode = 'concrete' THEN
    RETURN QUERY
    WITH _category_dists AS MATERIALIZED (
      SELECT
        ct.id          AS content_type_id,
        ct.name        AS content_type_name,
        ct.description AS content_type_description,
        (ct.embedding <=> p_query_emb)::double precision AS cat_dist
      FROM content_types ct
      WHERE ct.org_id    = p_org_id
        AND ct.is_active = true
        AND ct.embedding IS NOT NULL
    ),
    lex AS (
      SELECT
        uf.id AS doc_id,
        ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], uf.tsv, v_tsquery) AS tsv_score
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
      ORDER BY ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], uf.tsv, v_tsquery) DESC
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
      cd.content_type_name                   AS content_type_name,
      cd.content_type_description            AS content_type_description,
      uf.created_at                          AS uploaded_at,
      uf.created_at::date                    AS created_at,
      (uf.summary_embedding <=> p_query_emb) AS vec_dist,
      l.tsv_score                            AS tsv_score,
      'concrete'::text                       AS mode_used,
      (
        (uf.summary_embedding <=> p_query_emb) - COALESCE(
          (
            CASE
              WHEN uf.content_type_id IS NOT NULL THEN (1 - cd.cat_dist)
              WHEN uf.comment_embedding IS NOT NULL THEN (1 - (uf.comment_embedding <=> p_query_emb))
              ELSE NULL::double precision
            END
          ) * 0.1,
          0::double precision
        )
      )::double precision                    AS rank_score
    FROM lex l
    JOIN user_files uf ON uf.id = l.doc_id
    LEFT JOIN _category_dists cd ON cd.content_type_id = uf.content_type_id
    WHERE uf.summary_embedding IS NOT NULL AND l.tsv_score > 0
    ORDER BY l.tsv_score DESC,
             (uf.summary_embedding <=> p_query_emb) - COALESCE(
               (
                 CASE
                   WHEN uf.content_type_id IS NOT NULL THEN (1 - cd.cat_dist)
                   WHEN uf.comment_embedding IS NOT NULL THEN (1 - (uf.comment_embedding <=> p_query_emb))
                   ELSE NULL::double precision
                 END
               ) * 0.1,
               0::double precision
             ) ASC
    LIMIT p_top_k;

  ELSE
    RETURN QUERY
    WITH _category_dists AS MATERIALIZED (
      SELECT
        ct.id          AS content_type_id,
        ct.name        AS content_type_name,
        ct.description AS content_type_description,
        (ct.embedding <=> p_query_emb)::double precision AS cat_dist
      FROM content_types ct
      WHERE ct.org_id    = p_org_id
        AND ct.is_active = true
        AND ct.embedding IS NOT NULL
    ),
    eligible_files AS (
      SELECT
        uf.id,
        uf.content_type_id,
        uf.summary_embedding,
        uf.comment_embedding
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
    ),
    summary_candidates AS (
      SELECT ef.id AS doc_id
      FROM eligible_files ef
      ORDER BY ef.summary_embedding <=> p_query_emb
      LIMIT v_pool
    ),
    category_candidates AS (
      SELECT ef.id AS doc_id
      FROM eligible_files ef
      JOIN _category_dists cd ON cd.content_type_id = ef.content_type_id
      ORDER BY cd.cat_dist ASC
      LIMIT v_pool
    ),
    manual_comment_candidates AS (
      SELECT ef.id AS doc_id
      FROM eligible_files ef
      WHERE ef.content_type_id IS NULL
        AND ef.comment_embedding IS NOT NULL
      ORDER BY ef.comment_embedding <=> p_query_emb
      LIMIT v_pool
    ),
    candidates AS (
      SELECT sc.doc_id FROM summary_candidates sc
      UNION
      SELECT cc.doc_id FROM category_candidates cc
      UNION
      SELECT mcc.doc_id FROM manual_comment_candidates mcc
    ),
    doc_vectors AS (
      SELECT
        uf.id                       AS doc_id,
        uf.org_id                   AS org_id,
        uf.user_id                  AS user_id,
        uf.original_filename        AS doc_title,
        uf.summary                  AS summary,
        uf.comment                  AS comment,
        uf.content_type_id          AS content_type_id,
        cd.content_type_name        AS content_type_name,
        cd.content_type_description AS content_type_description,
        uf.created_at               AS uploaded_at,
        uf.created_at::date         AS created_at,
        uf.tsv                      AS tsv,
        (uf.summary_embedding <=> p_query_emb) AS vec_dist,
        CASE
          WHEN uf.content_type_id IS NOT NULL THEN (1 - cd.cat_dist)
          WHEN uf.comment_embedding IS NOT NULL THEN (1 - (uf.comment_embedding <=> p_query_emb))
          ELSE NULL::double precision
        END AS context_score
      FROM candidates c
      JOIN user_files uf ON uf.id = c.doc_id
      LEFT JOIN _category_dists cd ON cd.content_type_id = uf.content_type_id
    ),
    scored AS (
      SELECT
        dv.*,
        CASE
          WHEN dv.tsv @@ v_tsquery THEN ts_rank(ARRAY[0.1, 0.3, 0.6, 1.0]::real[], dv.tsv, v_tsquery)
          ELSE 0
        END AS tsv_score,
        dv.vec_dist - COALESCE(dv.context_score * 0.1, 0::double precision) AS final_score
      FROM doc_vectors dv
    )
    SELECT
      s.doc_id                   AS doc_id,
      s.org_id                   AS org_id,
      s.user_id                  AS user_id,
      s.doc_title                AS doc_title,
      s.summary                  AS summary,
      s.comment                  AS comment,
      s.content_type_id          AS content_type_id,
      s.content_type_name        AS content_type_name,
      s.content_type_description AS content_type_description,
      s.uploaded_at              AS uploaded_at,
      s.created_at               AS created_at,
      s.vec_dist                 AS vec_dist,
      s.tsv_score                AS tsv_score,
      'abstract'::text           AS mode_used,
      s.final_score              AS rank_score
    FROM scored s
    WHERE s.vec_dist < 0.65 OR s.context_score > 0.35
    ORDER BY s.final_score ASC, s.tsv_score DESC
    LIMIT p_top_k;
  END IF;
END;
$$;

COMMENT ON FUNCTION search_related_docs(uuid, uuid, text, vector, integer, boolean, uuid, boolean, text, uuid) IS
  'p_search_mode chooses concrete TSV-first or abstract vector-first search. '
  'Query punctuation is normalized before tsquery generation to match filename TSV normalization. '
  'Both modes compute per-category/manual-comment context score as 1 - cosine distance and apply '
  'context score as vec_dist - context_score * 0.1 when context exists. '
  'rank_score is the lower-is-better adjusted distance used by abstract ordering and concrete tiebreaking. '
  'Abstract mode builds candidates from summary, category, and manual-comment embeddings; '
  'concrete mode uses TSV rank first, then context-adjusted vec_dist as tiebreaker. '
  'Files without category/manual-comment context rank by vec_dist only. '
  'p_is_admin allows org admins to search all active documents including private files. '
  'p_filter_user_id narrows to one file owner. '
  'p_exclude_images omits image files. '
  'p_content_type_id narrows results to one content type.';
