-- Migration 019: Three main-page agents (Размышлятор / Поиск по документам / Поиск по интернету)
-- Replaces admin_oss20 / admin_qwen3 / admin_glm system roles + users.
-- Fresh-install assumption: no user-generated history attached to old system users yet.

DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    role_reasoner_id UUID;
    role_doc_search_id UUID;
    role_web_search_id UUID;
BEGIN
    -- ============================================
    -- 1. Remove old admin_* system users and roles
    -- ============================================
    DELETE FROM users
    WHERE username IN ('ai_oss20', 'ai_qwen3', 'ai_glm')
      AND is_system = true;

    DELETE FROM roles
    WHERE code IN ('admin_oss20', 'admin_qwen3', 'admin_glm')
      AND org_id = rugpt_org_id;

    -- ============================================
    -- 2. Create new roles
    -- ============================================
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Размышлятор',
        'reasoner',
        'Агент для рассуждений, анализа и цепочек мыслей',
        'Вы — Размышлятор. Помогаете пользователю думать и рассуждать.',
        'qwen3:14b',
        'simple',
        '[]'::jsonb,
        'reasoner.md',
        true
    )
    RETURNING id INTO role_reasoner_id;

    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Поиск по документам',
        'doc_search',
        'Агент для поиска и ответов по документам организации',
        'Вы — помощник по поиску информации в документах организации.',
        'qwen3:14b',
        'simple',
        '["rag_search"]'::jsonb,
        'doc_search.md',
        true
    )
    RETURNING id INTO role_doc_search_id;

    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Поиск по интернету',
        'web_search',
        'Агент для поиска актуальной информации в интернете с учётом контекста организации',
        'Вы — помощник по поиску актуальной информации в интернете.',
        'qwen3:14b',
        'simple',
        '["web_search"]'::jsonb,
        'web_search.md',
        true
    )
    RETURNING id INTO role_web_search_id;

    -- ============================================
    -- 3. Create new system users bound to these roles
    -- ============================================
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'Размышлятор', 'reasoner', 'reasoner@rugpt.system', NULL,
        role_reasoner_id, false, true, true
    );

    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'Поиск по документам', 'doc_search', 'doc-search@rugpt.system', NULL,
        role_doc_search_id, false, true, true
    );

    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'Поиск по интернету', 'web_search', 'web-search@rugpt.system', NULL,
        role_web_search_id, false, true, true
    );
END $$;
