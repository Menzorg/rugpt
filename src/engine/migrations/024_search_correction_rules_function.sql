-- Migration 024: search_correction_rules SQL function
-- Dense-rank fusion over mem_embedding and user_message_embedding.
-- mem score weight: 0.8, user prompt score weight: 1.0 (no multiplier).

CREATE OR REPLACE FUNCTION search_correction_rules(
    p_mem_embedding         vector(1024),
    p_user_msg_embedding    vector(1024),
    p_top_k                 integer DEFAULT 3
)
RETURNS TABLE (
    id                      uuid,
    role_id                 uuid,
    mem_id                  uuid,
    mem_embedding           vector(1024),
    src_user_message_id     uuid,
    user_message_embedding  vector(1024),
    src_ai_response_id      uuid,
    user_correction_text    text,
    extracted_lesson        text,
    is_active               boolean,
    mem_dist                double precision,
    user_dist               double precision,
    r_mem                   bigint,
    r_user                  bigint,
    final_rank              double precision
)
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    v_pool integer;
BEGIN
    v_pool := GREATEST(p_top_k * 5, 25);

    RETURN QUERY
    WITH candidates AS (
        -- Pull a broad candidate pool ordered by combined raw distance
        SELECT
            cr.id,
            cr.role_id,
            cr.mem_id,
            cr.mem_embedding,
            cr.src_user_message_id,
            cr.user_message_embedding,
            cr.src_ai_response_id,
            cr.user_correction_text,
            cr.extracted_lesson,
            cr.is_active,
            (cr.mem_embedding          <=> p_mem_embedding)      AS mem_dist,
            (cr.user_message_embedding <=> p_user_msg_embedding) AS user_dist
        FROM correction_rules cr
        WHERE cr.mem_embedding IS NOT NULL
          AND cr.user_message_embedding IS NOT NULL
          AND cr.is_active
        ORDER BY
            (cr.mem_embedding <=> p_mem_embedding) +
            (cr.user_message_embedding <=> p_user_msg_embedding)
        LIMIT v_pool
    ),
    ranked AS (
        SELECT
            c.*,
            dense_rank() OVER (ORDER BY c.mem_dist  ASC)  AS r_mem,
            dense_rank() OVER (ORDER BY c.user_dist ASC)  AS r_user
        FROM candidates c
    )
    SELECT
        r.id,
        r.role_id,
        r.mem_id,
        r.mem_embedding,
        r.src_user_message_id,
        r.user_message_embedding,
        r.src_ai_response_id,
        r.user_correction_text,
        r.extracted_lesson,
        r.is_active,
        r.mem_dist,
        r.user_dist,
        r.r_mem,
        r.r_user,
        -- Weighted rank fusion: mem rank penalised by 0.8, user rank at full weight
        (0.8 * r.r_mem::double precision + 1.0 * r.r_user::double precision) AS final_rank
    FROM ranked r
    ORDER BY final_rank ASC
    LIMIT p_top_k;
END;
$$;
