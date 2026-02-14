-- RuGPT Phase 4: System Users for Admins
-- Creates a special "RuGPT" organization with universal AI assistant roles
-- and system users (one per model) that admins from any organization can chat with.

-- ============================================
-- Users: add is_system field
-- ============================================
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT false;

-- Index for quick system user lookup
CREATE INDEX IF NOT EXISTS idx_users_system ON users(is_system) WHERE is_system = true;

-- ============================================
-- Create RuGPT system organization
-- ============================================
-- Fixed UUID for predictability: '00000000-0000-0000-0000-000000000000'
INSERT INTO organizations (id, name, slug, description, is_active)
VALUES (
    '00000000-0000-0000-0000-000000000000'::uuid,
    'RuGPT',
    'rugpt-system',
    'Системная организация для AI-ассистентов',
    true
)
ON CONFLICT (id) DO UPDATE
SET
    name = EXCLUDED.name,
    slug = EXCLUDED.slug,
    description = EXCLUDED.description,
    updated_at = NOW();

-- ============================================
-- Create universal AI assistant roles in RuGPT org
-- ============================================

DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    role_gpt4_id UUID;
    role_qwen_id UUID;
    role_claude_id UUID;
BEGIN
    RAISE NOTICE 'Creating universal AI assistant roles in RuGPT organization';

    -- Create GPT-OSS 20B Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'GPT-OSS 20B Ассистент',
        'admin_oss20',
        'Универсальный AI-помощник на базе GPT-OSS 20B для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'gpt-oss:20b',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO role_gpt4_id;

    -- If already exists, fetch the id
    IF role_gpt4_id IS NULL THEN
        SELECT id INTO role_gpt4_id FROM roles WHERE org_id = rugpt_org_id AND code = 'admin_oss20';
    END IF;

    -- Create Qwen3 Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'Qwen3 Ассистент',
        'admin_qwen3',
        'Универсальный AI-помощник на базе Qwen3 для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'qwen3:14b',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO role_qwen_id;

    IF role_qwen_id IS NULL THEN
        SELECT id INTO role_qwen_id FROM roles WHERE org_id = rugpt_org_id AND code = 'admin_qwen3';
    END IF;

    -- Create GLM-4.7 Flash Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'GLM-4.7 Flash Ассистент',
        'admin_glm',
        'Универсальный AI-помощник на базе GLM-4.7 Flash для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'glm-4.7-flash',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO role_claude_id;

    IF role_claude_id IS NULL THEN
        SELECT id INTO role_claude_id FROM roles WHERE org_id = rugpt_org_id AND code = 'admin_glm';
    END IF;

    RAISE NOTICE 'Created roles - GPT-4: %, Qwen: %, Claude: %', role_gpt4_id, role_qwen_id, role_claude_id;

    -- ============================================
    -- Create system users (one per model)
    -- ============================================

    RAISE NOTICE 'Creating system users in RuGPT organization';

    -- System user for GPT-OSS 20B
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'AI GPT-OSS 20B', 'ai_oss20', 'ai-gpt4@rugpt.system', NULL,
        role_gpt4_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;

    -- System user for Qwen3
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'AI Qwen3', 'ai_qwen3', 'ai-qwen@rugpt.system', NULL,
        role_qwen_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;

    -- System user for GLM-4.7 Flash
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'AI GLM-4.7 Flash', 'ai_glm', 'ai-claude@rugpt.system', NULL,
        role_claude_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;

    RAISE NOTICE 'System users created successfully';
END $$;

-- ============================================
-- Comments for documentation
-- ============================================
COMMENT ON COLUMN users.is_system IS 'Indicates if this is a system user (AI assistant for admins). System users belong to RuGPT organization and are shared across all organizations.';
COMMENT ON INDEX idx_users_system IS 'Fast lookup for system users';

-- ============================================
-- Verify migration
-- ============================================
-- You can verify with:
-- SELECT u.name, u.is_system, r.name as role_name, r.model_name, o.name as org_name
-- FROM users u
-- LEFT JOIN roles r ON u.role_id = r.id
-- LEFT JOIN organizations o ON u.org_id = o.id
-- WHERE u.is_system = true;
