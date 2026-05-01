-- 031_chunk_context_by_index.sql
-- Add helper to fetch neighboring chunks around a known file_id + chunk_index.

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
  chunk_index integer,
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
        c.chunk_index AS chunk_index,
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
        l.chunk_index,
        (l.embedding <=> p_query_emb) AS vec_dist,
        l.tsv_score
      FROM lex l
    ),
    ranked AS (
      SELECT
        s.*,
        dense_rank() OVER (ORDER BY s.vec_dist ASC)::int   AS r_vec,
        dense_rank() OVER (ORDER BY s.tsv_score DESC)::int AS r_tsv
      FROM scored s
    )
    SELECT
      r.chunk_id    AS chunk_id,
      r.doc_id      AS doc_id,
      r.chunk_text  AS chunk_text,
      r.chunk_index AS chunk_index,
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
        c.chunk_index AS chunk_index,
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
      v.chunk_index AS chunk_index,
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
  p_query_emb vector(1024),
  p_top_k integer
)
RETURNS TABLE (
  chunk_id uuid,
  doc_id uuid,
  chunk_text text,
  chunk_index integer,
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
    c.chunk_index,
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
  chunk_index,
  vec_dist,
  tsv_score,
  r_vec,
  r_tsv,
  (r_vec + 0.3 * r_tsv) AS final_rank
FROM ranked
ORDER BY final_rank ASC
LIMIT p_top_k;
$$;

CREATE OR REPLACE FUNCTION search_rag(
  p_doc_id uuid,
  p_query text,
  p_query_emb vector(1024),
  p_top_k integer,
  p_method text
)
RETURNS TABLE (
  item_id uuid,
  doc_id uuid,
  text_content text,
  chunk_index integer,
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
      SELECT r.row_id, r.doc_id, r.row_text, NULL::integer, r.vec_dist, r.tsv_score, 'table_row'::text
      FROM search_concrete_table_rows(p_doc_id, p_query, p_query_emb, p_top_k) r;
    ELSIF p_method = 'abstract' THEN
      RETURN QUERY
      SELECT r.row_id, r.doc_id, r.row_text, NULL::integer, r.vec_dist, r.tsv_score, 'table_row'::text
      FROM search_abstract_table_rows(p_doc_id, p_query, p_query_emb, p_top_k) r;
    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;
  ELSE
    IF p_method = 'concrete' THEN
      RETURN QUERY
      SELECT c.chunk_id, c.doc_id, c.chunk_text, c.chunk_index, c.vec_dist, c.tsv_score, 'chunk'::text
      FROM search_concrete_chunks(p_doc_id, p_query, p_query_emb, p_top_k) c;
    ELSIF p_method = 'abstract' THEN
      RETURN QUERY
      SELECT c.chunk_id, c.doc_id, c.chunk_text, c.chunk_index, c.vec_dist, c.tsv_score, 'chunk'::text
      FROM search_abstract_chunks(p_doc_id, p_query, p_query_emb, p_top_k) c;
    ELSE
      RAISE EXCEPTION 'Unknown method: %', p_method;
    END IF;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION get_expanded_context_by_index(
  p_doc_id uuid,
  p_chunk_index integer,
  p_distance integer DEFAULT 1
)
RETURNS TABLE (
  id uuid,
  file_id uuid,
  chunk_text text,
  metadata jsonb,
  chunk_index integer
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
  v_center_chunk_id uuid;
BEGIN
  SELECT c.id
  INTO v_center_chunk_id
  FROM chunks c
  WHERE c.file_id = p_doc_id
    AND c.chunk_index = p_chunk_index
  LIMIT 1;

  IF v_center_chunk_id IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
  SELECT
    c.id,
    c.file_id,
    c.chunk_text,
    c.metadata,
    c.chunk_index
  FROM chunks c
  WHERE c.file_id = p_doc_id
    AND c.chunk_index BETWEEN
        p_chunk_index - p_distance
        AND
        p_chunk_index + p_distance
  ORDER BY c.chunk_index;
END;
$$;
