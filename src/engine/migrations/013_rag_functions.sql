-- 013_rag_functions.sql
-- Grouped function migrations for RAG retrieval functions.

CREATE OR REPLACE FUNCTION search_concrete_chunks(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
  p_top_k integer,
  p_tsv_weight double precision DEFAULT 1.0   -- TSV weight in final fusion
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

  -- For concrete mode, consider plainto_tsquery if websearch operators hurt recall:
  v_tsquery := websearch_to_tsquery('russian', p_query);

  SELECT count(*)
  INTO v_count
  FROM chunks c
  WHERE c.doc_id = p_doc_id
    AND c.tsv @@ v_tsquery;

  IF v_count > 0 THEN
    RETURN QUERY
    WITH lex AS (
      -- 1) TSV-first candidate set (still important for speed/precision)
      SELECT
        c.id         AS chunk_id,
        c.doc_id     AS doc_id,
        c.chunk_text AS chunk_text,
        c.embedding  AS embedding,
        ts_rank_cd(c.tsv, v_tsquery) AS tsv_score
      FROM chunks c
      WHERE c.doc_id = p_doc_id
        AND c.tsv @@ v_tsquery
      ORDER BY tsv_score DESC
      LIMIT v_pool
    ),
    scored AS (
      -- 2) Add vector distance for the same candidates
      SELECT
        l.chunk_id,
        l.doc_id,
        l.chunk_text,
        (l.embedding <=> p_query_emb) AS vec_dist,
        l.tsv_score
      FROM lex l
    ),
    ranked AS (
      -- 3) Convert both signals to ranks (stable scale)
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
    -- Fallback: vector-only (still returns ranks/final_rank for consistent schema)
    RETURN QUERY
    WITH vec AS (
      SELECT
        c.id         AS chunk_id,
        c.doc_id     AS doc_id,
        c.chunk_text AS chunk_text,
        (c.embedding <=> p_query_emb) AS vec_dist
      FROM chunks c
      WHERE c.doc_id = p_doc_id
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

CREATE OR REPLACE FUNCTION search_abstract_chunks(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
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

-- Step 1: Vector first
vec AS (
  SELECT
    c.id AS chunk_id,
    c.doc_id,
    c.chunk_text,
    (c.embedding <=> p_query_emb) AS vec_dist
  FROM chunks c
  WHERE c.doc_id = p_doc_id
  ORDER BY c.embedding <=> p_query_emb
  LIMIT GREATEST(p_top_k * 10, 50)
),

-- Step 2: Compute TSV score inside vector candidates
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

-- Step 3: Rank fusion (normalization workaround)
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
  (r_vec + 0.3 * r_tsv) AS final_rank -- vector score + little weighted tsv score after kinda normalization made with dense_rank()
FROM ranked
ORDER BY final_rank ASC
LIMIT p_top_k;
$$;

CREATE OR REPLACE FUNCTION search_concrete_table_rows(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
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

  ------------------------------------------------------------------
  -- parameters
  ------------------------------------------------------------------

  v_pool := GREATEST(p_top_k * 10, 50);

  v_tsquery := plainto_tsquery('russian', p_query);

  ------------------------------------------------------------------
  -- TSV candidate set
  ------------------------------------------------------------------

  RETURN QUERY
  WITH lex AS (

    SELECT
      tr.id AS row_id,
      tr.doc_id,
      tr.row_text,
      tr.embedding,
      ts_rank_cd(tr.tsv, v_tsquery) AS tsv_score
    FROM tables_rows_chunks tr
    WHERE tr.doc_id = p_doc_id
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

CREATE OR REPLACE FUNCTION search_abstract_table_rows(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
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

  ------------------------------------------------------------------
  -- parameters
  ------------------------------------------------------------------

  v_pool := GREATEST(p_top_k * 10, 50);

  v_tsquery := plainto_tsquery('russian', p_query);

  ------------------------------------------------------------------
  -- vector candidate set
  ------------------------------------------------------------------

  RETURN QUERY
  WITH vec AS (

    SELECT
      tr.id AS row_id,
      tr.doc_id,
      tr.row_text,
      (tr.embedding <=> p_query_emb) AS vec_dist
    FROM tables_rows_chunks tr
    WHERE tr.doc_id = p_doc_id
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

CREATE OR REPLACE FUNCTION search_related_docs(
  p_org_id uuid,                 -- Organization scope
  p_user_id uuid,                -- User performing search
  p_query text,                  -- Raw search query
  p_query_emb vector(768),       -- Precomputed embedding of query
  p_top_k integer                -- Number of docs to return
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
  mode_used text                 -- 'concrete' or 'abstract'
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_tsquery tsquery;             -- Parsed full-text query
  v_count integer;               -- Number of lexical matches
  v_pool integer;                -- Candidate pool size before final limit
BEGIN
  ------------------------------------------------------------------
  -- 1) Precompute parameters
  ------------------------------------------------------------------
  v_pool := GREATEST(p_top_k * 10, 50);
  v_tsquery := plainto_tsquery('russian', p_query);

  ------------------------------------------------------------------
  -- 2) Check if concrete (lexical) matches exist in accessible docs
  ------------------------------------------------------------------
  SELECT count(*)
  INTO v_count
  FROM rag_docs d
  WHERE d.org_id = p_org_id
    AND (d.user_id IS NULL OR d.user_id = p_user_id)
    AND d.tsv @@ v_tsquery;

  ------------------------------------------------------------------
  -- 3) CONCRETE branch: TSV-first -> vector rerank
  ------------------------------------------------------------------
  IF v_count > 0 THEN
    RETURN QUERY
    WITH lex AS (
      SELECT
        d.doc_id AS doc_id,
        ts_rank_cd(d.tsv, v_tsquery) AS tsv_score
      FROM rag_docs d
      WHERE d.org_id = p_org_id
        AND (d.user_id IS NULL OR d.user_id = p_user_id)
        AND d.tsv @@ v_tsquery
      ORDER BY tsv_score DESC
      LIMIT v_pool
    )
    SELECT
      d.doc_id      AS doc_id,
      d.org_id      AS org_id,
      d.user_id     AS user_id,
      d.doc_title   AS doc_title,
      d.summary     AS summary,
      d.uploaded_at AS uploaded_at,
      d.created_at  AS created_at,
      (d.summary_embedding <=> p_query_emb) AS vec_dist,
      l.tsv_score   AS tsv_score,
      'concrete'::text AS mode_used
    FROM lex l
    JOIN rag_docs d ON d.doc_id = l.doc_id
    WHERE d.summary_embedding IS NOT NULL
    ORDER BY vec_dist ASC
    LIMIT p_top_k;

  ------------------------------------------------------------------
  -- 4) ABSTRACT fallback: vector-first -> lexical scoring -> rank fusion
  ------------------------------------------------------------------
  ELSE
    RETURN QUERY
    WITH vec AS (
      SELECT
        d.doc_id      AS doc_id,
        d.org_id      AS org_id,
        d.user_id     AS user_id,
        d.doc_title   AS doc_title,
        d.summary     AS summary,
        d.uploaded_at AS uploaded_at,
        d.created_at  AS created_at,
        (d.summary_embedding <=> p_query_emb) AS vec_dist
      FROM rag_docs d
      WHERE d.org_id = p_org_id
        AND (d.user_id IS NULL OR d.user_id = p_user_id)
        AND d.summary_embedding IS NOT NULL
      ORDER BY d.summary_embedding <=> p_query_emb
      LIMIT v_pool
    ),
    scored AS (
      SELECT
        v.doc_id      AS doc_id,
        v.org_id      AS org_id,
        v.user_id     AS user_id,
        v.doc_title   AS doc_title,
        v.summary     AS summary,
        v.uploaded_at AS uploaded_at,
        v.created_at  AS created_at,
        v.vec_dist    AS vec_dist,
        CASE
          WHEN d.tsv @@ v_tsquery THEN ts_rank_cd(d.tsv, v_tsquery)
          ELSE 0
        END AS tsv_score
      FROM vec v
      JOIN rag_docs d ON d.doc_id = v.doc_id
    ),
    ranked AS (
      SELECT
        s.*,
        dense_rank() OVER (ORDER BY s.vec_dist ASC)   AS r_vec,
        dense_rank() OVER (ORDER BY s.tsv_score DESC) AS r_tsv
      FROM scored s
    )
    SELECT
      r.doc_id      AS doc_id,
      r.org_id      AS org_id,
      r.user_id     AS user_id,
      r.doc_title   AS doc_title,
      r.summary     AS summary,
      r.uploaded_at AS uploaded_at,
      r.created_at  AS created_at,
      r.vec_dist    AS vec_dist,
      r.tsv_score   AS tsv_score,
      'abstract'::text AS mode_used
    FROM ranked r
    WHERE r.vec_dist < 0.5
    ORDER BY (r.r_vec + 0.3 * r.r_tsv) ASC
    LIMIT p_top_k;
  END IF;

END;
$$;


CREATE OR REPLACE FUNCTION search_rag(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
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

  ------------------------------------------------------------------
  -- 1. Detect document type
  ------------------------------------------------------------------

  SELECT d.is_table
  INTO v_is_table
  FROM rag_docs d
  WHERE d.doc_id = p_doc_id;

  IF v_is_table IS NULL THEN
    RAISE EXCEPTION 'Document % not found', p_doc_id;
  END IF;

  ------------------------------------------------------------------
  -- 2. TABLE documents
  ------------------------------------------------------------------

  IF v_is_table THEN

    IF p_method = 'concrete' THEN

      RETURN QUERY
      SELECT
        r.row_id      AS item_id,
        r.doc_id      AS doc_id,
        r.row_text    AS text_content,
        r.vec_dist,
        r.tsv_score,
        'table_row'::text AS source_type
      FROM search_concrete_table_rows(
        p_doc_id,
        p_query,
        p_query_emb,
        p_top_k
      ) r;

    ELSIF p_method = 'abstract' THEN

      RETURN QUERY
      SELECT
        r.row_id      AS item_id,
        r.doc_id      AS doc_id,
        r.row_text    AS text_content,
        r.vec_dist,
        r.tsv_score,
        'table_row'::text AS source_type
      FROM search_abstract_table_rows(
        p_doc_id,
        p_query,
        p_query_emb,
        p_top_k
      ) r;

    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;


  ------------------------------------------------------------------
  -- 3. NORMAL TEXT documents
  ------------------------------------------------------------------

  ELSE

    IF p_method = 'concrete' THEN

      RETURN QUERY
      SELECT
        c.chunk_id    AS item_id,
        c.doc_id      AS doc_id,
        c.chunk_text  AS text_content,
        c.vec_dist,
        c.tsv_score,
        'chunk'::text AS source_type
      FROM search_concrete_chunks(
        p_doc_id,
        p_query,
        p_query_emb,
        p_top_k
      ) c;

    ELSIF p_method = 'abstract' THEN

      RETURN QUERY
      SELECT
        c.chunk_id    AS item_id,
        c.doc_id      AS doc_id,
        c.chunk_text  AS text_content,
        c.vec_dist,
        c.tsv_score,
        'chunk'::text AS source_type
      FROM search_abstract_chunks(
        p_doc_id,
        p_query,
        p_query_emb,
        p_top_k
      ) c;

    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;

  END IF;

END;
$$;


CREATE OR REPLACE FUNCTION get_expanded_context(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(768),
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

  --------------------------------------------------------------------
  -- 1. Find best chunk using concrete search
  --------------------------------------------------------------------

  SELECT s.chunk_id
  INTO v_center_chunk_id
  FROM search_concrete_chunks(
    p_doc_id,
    p_query,
    p_query_emb,
    1
  ) s
  LIMIT 1;

  IF v_center_chunk_id IS NULL THEN
    RETURN;
  END IF;


  --------------------------------------------------------------------
  -- 2. Get chunk_index of the best chunk
  --------------------------------------------------------------------

  SELECT c.chunk_index
  INTO v_center_index
  FROM chunks c
  WHERE c.id = v_center_chunk_id
    AND c.doc_id = p_doc_id;


  --------------------------------------------------------------------
  -- 3. Return window around the chunk
  --------------------------------------------------------------------

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
  WHERE c.doc_id = p_doc_id
    AND c.chunk_index BETWEEN
        v_center_index - p_distance
        AND
        v_center_index + p_distance;

END;
$$;
