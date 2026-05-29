-- 048_fix_search_rag_row_index.sql
-- Fix search_rag to return row_index from tables_rows_chunks instead of NULL.
-- Rebuilds search_concrete_table_rows and search_abstract_table_rows with row_index
-- in their return type, then rebuilds search_rag to propagate it.

DROP FUNCTION IF EXISTS search_rag(uuid, text, vector(1024), integer, text);
DROP FUNCTION IF EXISTS get_expanded_context_by_index(uuid, integer, integer);
DROP FUNCTION IF EXISTS search_concrete_table_rows(uuid, text, vector(1024), integer, double precision);
DROP FUNCTION IF EXISTS search_abstract_table_rows(uuid, text, vector(1024), integer, double precision);

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
  row_index integer,
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
      tr.row_index,
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
      l.row_index,
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
    r.row_index,
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
  p_query_emb vector(1024),
  p_top_k integer,
  p_tsv_weight double precision DEFAULT 0.3
)
RETURNS TABLE (
  row_id uuid,
  doc_id uuid,
  row_text text,
  row_index integer,
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
      tr.row_index,
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
      v.row_index,
      v.vec_dist,
      CASE
        WHEN tr.tsv @@ v_tsquery
        THEN ts_rank_cd(tr.tsv, v_tsquery)
        ELSE 0
      END AS tsv_score
    FROM vec v
    JOIN tables_rows_chunks tr ON tr.id = v.row_id
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
    r.row_index,
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
      SELECT r.row_id, r.doc_id, r.row_text, r.row_index, r.vec_dist, r.tsv_score, 'table_row'::text
      FROM search_concrete_table_rows(p_doc_id, p_query, p_query_emb, p_top_k) r;
    ELSIF p_method = 'abstract' THEN
      RETURN QUERY
      SELECT r.row_id, r.doc_id, r.row_text, r.row_index, r.vec_dist, r.tsv_score, 'table_row'::text
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
    RETURN QUERY
    SELECT
      tr.id,
      tr.file_id,
      tr.row_text   AS chunk_text,
      tr.metadata,
      tr.row_index  AS chunk_index
    FROM tables_rows_chunks tr
    WHERE tr.file_id = p_doc_id
      AND tr.row_index BETWEEN p_chunk_index - p_distance AND p_chunk_index + p_distance
    ORDER BY tr.row_index;
  ELSE
    RETURN QUERY
    SELECT
      c.id,
      c.file_id,
      c.chunk_text,
      c.metadata,
      c.chunk_index
    FROM chunks c
    WHERE c.file_id = p_doc_id
      AND c.chunk_index BETWEEN p_chunk_index - p_distance AND p_chunk_index + p_distance
    ORDER BY c.chunk_index;
  END IF;
END;
$$;
