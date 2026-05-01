CREATE OR REPLACE FUNCTION search_related_docs(
  p_org_id uuid,
  p_user_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer
)
RETURNS TABLE (
  doc_id uuid,
  org_id uuid,
  user_id uuid,
  doc_title varchar(500),
  summary text,
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
  v_count integer;
  v_pool integer;
BEGIN
  v_pool := GREATEST(p_top_k * 10, 50);
  v_tsquery := plainto_tsquery('russian', p_query);

  -- Count TSV matches directly on user_files (filename A + summary B)
  SELECT count(*)
  INTO v_count
  FROM user_files uf
  WHERE uf.org_id    = p_org_id
    AND (uf.is_public OR uf.user_id = p_user_id)
    AND uf.is_active = true
    AND uf.tsv @@ v_tsquery;

  IF v_count > 0 THEN
    -- TSV-first: rank by text match, re-sort by vector distance
    RETURN QUERY
    WITH lex AS (
      SELECT
        uf.id AS doc_id,
        ts_rank_cd(uf.tsv, v_tsquery) AS tsv_score
      FROM user_files uf
      WHERE uf.org_id    = p_org_id
        AND (uf.is_public OR uf.user_id = p_user_id)
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
      uf.created_at                          AS uploaded_at,
      uf.created_at::date                    AS created_at,
      (uf.summary_embedding <=> p_query_emb) AS vec_dist,
      l.tsv_score                            AS tsv_score,
      'concrete'::text                       AS mode_used
    FROM lex l
    JOIN user_files uf ON uf.id = l.doc_id
    WHERE uf.summary_embedding IS NOT NULL
    ORDER BY vec_dist ASC
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
        AND (uf.is_public OR uf.user_id = p_user_id)
        AND uf.is_active = true
        AND uf.summary_embedding IS NOT NULL
      ORDER BY uf.summary_embedding <=> p_query_emb
      LIMIT v_pool
    ),
    scored AS (
      SELECT
        v.*,
        CASE
          WHEN uf.tsv @@ v_tsquery THEN ts_rank_cd(uf.tsv, v_tsquery)
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
      uf.created_at           AS uploaded_at,
      uf.created_at::date     AS created_at,
      r.vec_dist              AS vec_dist,
      r.tsv_score             AS tsv_score,
      'abstract'::text        AS mode_used
    FROM ranked r
    JOIN user_files uf ON uf.id = r.doc_id
    WHERE r.vec_dist < 0.8
    ORDER BY (r.r_vec + 0.3 * r.r_tsv) ASC
    LIMIT p_top_k;
  END IF;
END;
$$;