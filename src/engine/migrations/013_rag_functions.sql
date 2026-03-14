-- 013_rag_functions.sql
-- Hybrid search functions for RAG retrieval (vector + TSV rank fusion)

-- =================================================================
-- 1. search_concrete_chunks: TSV-first hybrid search in chunks
-- =================================================================

CREATE OR REPLACE FUNCTION search_concrete_chunks(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_tsv_weight double precision DEFAULT 1.0
)
RETURNS TABLE (
  chunk_id uuid,
  doc_id uuid,
  chunk_text text,
  vec_dist double precision,
  tsv_score real,
  r_vec int,
  r_tsv int,
  final_rank double precision
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
  v_tsquery := websearch_to_tsquery('russian', p_query);

  SELECT count(*)
  INTO v_count
  FROM chunks c
  WHERE c.file_id = p_doc_id
    AND c.tsv @@ v_tsquery;

  IF v_count > 0 THEN
    RETURN QUERY
    WITH lex AS (
      SELECT
        c.id         AS chunk_id,
        c.file_id    AS doc_id,
        c.chunk_text AS chunk_text,
        c.embedding  AS embedding,
        ts_rank_cd(c.tsv, v_tsquery) AS tsv_score
      FROM chunks c
      WHERE c.file_id = p_doc_id
        AND c.tsv @@ v_tsquery
      ORDER BY tsv_score DESC
      LIMIT v_pool
    ),
    scored AS (
      SELECT
        l.chunk_id,
        l.doc_id,
        l.chunk_text,
        (l.embedding <=> p_query_emb) AS vec_dist,
        l.tsv_score
      FROM lex l
    ),
    ranked AS (
      SELECT
        s.*,
        dense_rank() OVER (ORDER BY s.vec_dist ASC)::int      AS r_vec,
        dense_rank() OVER (ORDER BY s.tsv_score DESC)::int    AS r_tsv
      FROM scored s
    )
    SELECT
      r.chunk_id    AS chunk_id,
      r.doc_id      AS doc_id,
      r.chunk_text  AS chunk_text,
      r.vec_dist    AS vec_dist,
      r.tsv_score   AS tsv_score,
      r.r_vec       AS r_vec,
      r.r_tsv       AS r_tsv,
      (r.r_vec + p_tsv_weight * r.r_tsv) AS final_rank
    FROM ranked r
    ORDER BY final_rank ASC
    LIMIT p_top_k;

  ELSE
    RETURN QUERY
    WITH vec AS (
      SELECT
        c.id         AS chunk_id,
        c.file_id    AS doc_id,
        c.chunk_text AS chunk_text,
        (c.embedding <=> p_query_emb) AS vec_dist
      FROM chunks c
      WHERE c.file_id = p_doc_id
      ORDER BY c.embedding <=> p_query_emb
      LIMIT p_top_k
    )
    SELECT
      v.chunk_id    AS chunk_id,
      v.doc_id      AS doc_id,
      v.chunk_text  AS chunk_text,
      v.vec_dist    AS vec_dist,
      0::real       AS tsv_score,
      dense_rank() OVER (ORDER BY v.vec_dist ASC)::int AS r_vec,
      1::int        AS r_tsv,
      dense_rank() OVER (ORDER BY v.vec_dist ASC)::double precision AS final_rank
    FROM vec v
    ORDER BY v.vec_dist ASC;
  END IF;
END;
$$;

-- =================================================================
-- 2. search_abstract_chunks: vector-first hybrid search in chunks
-- =================================================================

CREATE OR REPLACE FUNCTION search_abstract_chunks(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer
)
RETURNS TABLE (
  chunk_id uuid,
  doc_id uuid,
  chunk_text text,
  vec_dist double precision,
  tsv_score real,
  r_vec int,
  r_tsv int,
  final_rank double precision
)
LANGUAGE sql
STABLE
AS $$
WITH q AS (
  SELECT websearch_to_tsquery('russian', p_query) AS tsq
),
vec AS (
  SELECT
    c.id AS chunk_id,
    c.file_id AS doc_id,
    c.chunk_text,
    (c.embedding <=> p_query_emb) AS vec_dist
  FROM chunks c
  WHERE c.file_id = p_doc_id
  ORDER BY c.embedding <=> p_query_emb
  LIMIT GREATEST(p_top_k * 10, 50)
),
scored AS (
  SELECT
    v.*,
    CASE
      WHEN c.tsv @@ q.tsq THEN ts_rank_cd(c.tsv, q.tsq)
      ELSE 0
    END AS tsv_score
  FROM vec v
  JOIN chunks c ON c.id = v.chunk_id
  CROSS JOIN q
),
ranked AS (
  SELECT
    *,
    dense_rank() OVER (ORDER BY vec_dist ASC)::bigint AS r_vec,
    dense_rank() OVER (ORDER BY tsv_score DESC)::bigint AS r_tsv
  FROM scored
)
SELECT
  chunk_id,
  doc_id,
  chunk_text,
  vec_dist,
  tsv_score,
  r_vec,
  r_tsv,
  (r_vec + 0.3 * r_tsv) AS final_rank
FROM ranked
ORDER BY final_rank ASC
LIMIT p_top_k;
$$;

-- =================================================================
-- 3. search_concrete_table_rows: TSV-first hybrid for table rows
-- =================================================================

CREATE OR REPLACE FUNCTION search_concrete_table_rows(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_tsv_weight double precision DEFAULT 1.0
)
RETURNS TABLE (
  row_id uuid,
  doc_id uuid,
  row_text text,
  vec_dist double precision,
  tsv_score real,
  r_vec bigint,
  r_tsv bigint,
  final_rank double precision
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

  RETURN QUERY
  WITH lex AS (
    SELECT
      tr.id AS row_id,
      tr.file_id AS doc_id,
      tr.row_text,
      tr.embedding,
      ts_rank_cd(tr.tsv, v_tsquery) AS tsv_score
    FROM tables_rows_chunks tr
    WHERE tr.file_id = p_doc_id
      AND tr.tsv @@ v_tsquery
    ORDER BY tsv_score DESC
    LIMIT v_pool
  ),
  scored AS (
    SELECT
      l.row_id,
      l.doc_id,
      l.row_text,
      (l.embedding <=> p_query_emb) AS vec_dist,
      l.tsv_score
    FROM lex l
  ),
  ranked AS (
    SELECT
      s.*,
      dense_rank() OVER (ORDER BY s.vec_dist ASC)::bigint AS r_vec,
      dense_rank() OVER (ORDER BY s.tsv_score DESC)::bigint AS r_tsv
    FROM scored s
  )
  SELECT
    r.row_id,
    r.doc_id,
    r.row_text,
    r.vec_dist,
    r.tsv_score,
    r.r_vec,
    r.r_tsv,
    (r.r_vec + p_tsv_weight * r.r_tsv) AS final_rank
  FROM ranked r
  ORDER BY final_rank ASC
  LIMIT p_top_k;
END;
$$;

-- =================================================================
-- 4. search_abstract_table_rows: vector-first hybrid for table rows
-- =================================================================

CREATE OR REPLACE FUNCTION search_abstract_table_rows(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_tsv_weight double precision DEFAULT 0.3
)
RETURNS TABLE (
  row_id uuid,
  doc_id uuid,
  row_text text,
  vec_dist double precision,
  tsv_score real,
  r_vec bigint,
  r_tsv bigint,
  final_rank double precision
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

  RETURN QUERY
  WITH vec AS (
    SELECT
      tr.id AS row_id,
      tr.file_id AS doc_id,
      tr.row_text,
      (tr.embedding <=> p_query_emb) AS vec_dist
    FROM tables_rows_chunks tr
    WHERE tr.file_id = p_doc_id
    ORDER BY tr.embedding <=> p_query_emb
    LIMIT v_pool
  ),
  scored AS (
    SELECT
      v.row_id,
      v.doc_id,
      v.row_text,
      v.vec_dist,
      CASE
        WHEN tr.tsv @@ v_tsquery
        THEN ts_rank_cd(tr.tsv, v_tsquery)
        ELSE 0
      END AS tsv_score
    FROM vec v
    JOIN tables_rows_chunks tr
      ON tr.id = v.row_id
  ),
  ranked AS (
    SELECT
      s.*,
      dense_rank() OVER (ORDER BY s.vec_dist ASC)::bigint AS r_vec,
      dense_rank() OVER (ORDER BY s.tsv_score DESC)::bigint AS r_tsv
    FROM scored s
  )
  SELECT
    r.row_id,
    r.doc_id,
    r.row_text,
    r.vec_dist,
    r.tsv_score,
    r.r_vec,
    r.r_tsv,
    (r.r_vec + p_tsv_weight * r.r_tsv) AS final_rank
  FROM ranked r
  ORDER BY final_rank ASC
  LIMIT p_top_k;
END;
$$;

-- =================================================================
-- 5. search_related_docs: find relevant documents for a query
--    Queries user_files directly (summary, tsv, summary_embedding).
-- =================================================================

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
  doc_title text,
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
    AND (uf.user_id IS NULL OR uf.user_id = p_user_id)
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
        AND (uf.user_id IS NULL OR uf.user_id = p_user_id)
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
        AND (uf.user_id IS NULL OR uf.user_id = p_user_id)
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
    WHERE r.vec_dist < 0.5
    ORDER BY (r.r_vec + 0.3 * r.r_tsv) ASC
    LIMIT p_top_k;
  END IF;
END;
$$;

-- =================================================================
-- 6. search_rag: unified dispatcher (chunks vs table rows)
--    Reads is_table from user_files (p_doc_id is user_files.id).
-- =================================================================

CREATE OR REPLACE FUNCTION search_rag(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_method text   -- 'abstract' | 'concrete'
)
RETURNS TABLE (
  item_id uuid,
  doc_id uuid,
  text_content text,
  vec_dist double precision,
  tsv_score real,
  source_type text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_is_table boolean;
BEGIN
  -- is_table lives in user_files; p_doc_id is user_files.id
  SELECT uf.is_table
  INTO v_is_table
  FROM user_files uf
  WHERE uf.id = p_doc_id;

  IF v_is_table IS NULL THEN
    RAISE EXCEPTION 'File % not found', p_doc_id;
  END IF;

  IF v_is_table THEN
    IF p_method = 'concrete' THEN
      RETURN QUERY
      SELECT r.row_id, r.doc_id, r.row_text, r.vec_dist, r.tsv_score, 'table_row'::text
      FROM search_concrete_table_rows(p_doc_id, p_query, p_query_emb, p_top_k) r;
    ELSIF p_method = 'abstract' THEN
      RETURN QUERY
      SELECT r.row_id, r.doc_id, r.row_text, r.vec_dist, r.tsv_score, 'table_row'::text
      FROM search_abstract_table_rows(p_doc_id, p_query, p_query_emb, p_top_k) r;
    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;
  ELSE
    IF p_method = 'concrete' THEN
      RETURN QUERY
      SELECT c.chunk_id, c.doc_id, c.chunk_text, c.vec_dist, c.tsv_score, 'chunk'::text
      FROM search_concrete_chunks(p_doc_id, p_query, p_query_emb, p_top_k) c;
    ELSIF p_method = 'abstract' THEN
      RETURN QUERY
      SELECT c.chunk_id, c.doc_id, c.chunk_text, c.vec_dist, c.tsv_score, 'chunk'::text
      FROM search_abstract_chunks(p_doc_id, p_query, p_query_emb, p_top_k) c;
    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;
  END IF;
END;
$$;

-- =================================================================
-- 7. get_expanded_context: window around best chunk
-- =================================================================

CREATE OR REPLACE FUNCTION get_expanded_context(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_distance integer DEFAULT 1
)
RETURNS TABLE (
  doc_id uuid,
  center_chunk_id uuid,
  center_chunk_index integer,
  context_text text
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_center_chunk_id uuid;
  v_center_index integer;
BEGIN
  SELECT s.chunk_id
  INTO v_center_chunk_id
  FROM search_concrete_chunks(p_doc_id, p_query, p_query_emb, 1) s
  LIMIT 1;

  IF v_center_chunk_id IS NULL THEN
    RETURN;
  END IF;

  SELECT c.chunk_index
  INTO v_center_index
  FROM chunks c
  WHERE c.id = v_center_chunk_id
    AND c.file_id = p_doc_id;

  RETURN QUERY
  SELECT
    p_doc_id AS doc_id,
    v_center_chunk_id AS center_chunk_id,
    v_center_index AS center_chunk_index,
    string_agg(
      c.chunk_text,
      E'\n\n-----\n\n'
      ORDER BY c.chunk_index
    ) AS context_text
  FROM chunks c
  WHERE c.file_id = p_doc_id
    AND c.chunk_index BETWEEN
        v_center_index - p_distance
        AND
        v_center_index + p_distance;
END;
$$;
