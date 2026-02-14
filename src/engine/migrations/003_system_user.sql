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

    -- Create GPT-4 Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'GPT-4 Ассистент',
        'admin_gpt4',
        'Универсальный AI-помощник на базе GPT-4 для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'gpt-4',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO UPDATE
    SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        model_name = EXCLUDED.model_name,
        prompt_file = EXCLUDED.prompt_file,
        tools = EXCLUDED.tools,
        updated_at = NOW()
    RETURNING id INTO role_gpt4_id;

    -- Create Qwen Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'Qwen Ассистент',
        'admin_qwen',
        'Универсальный AI-помощник на базе Qwen для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'qwen2.5:7b',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO UPDATE
    SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        model_name = EXCLUDED.model_name,
        prompt_file = EXCLUDED.prompt_file,
        tools = EXCLUDED.tools,
        updated_at = NOW()
    RETURNING id INTO role_qwen_id;

    -- Create Claude Assistant role
    INSERT INTO roles (org_id, name, code, description, system_prompt, model_name, agent_type, tools, prompt_file, is_active)
    VALUES (
        rugpt_org_id,
        'Claude Ассистент',
        'admin_claude',
        'Универсальный AI-помощник на базе Claude для руководителей',
        'Вы — персональный AI-ассистент руководителя компании.',
        'claude-3',
        'simple',
        '["calendar_create", "calendar_query", "rag_search", "web_search", "role_call"]'::jsonb,
        'admin_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO UPDATE
    SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        model_name = EXCLUDED.model_name,
        prompt_file = EXCLUDED.prompt_file,
        tools = EXCLUDED.tools,
        updated_at = NOW()
    RETURNING id INTO role_claude_id;

    RAISE NOTICE 'Created roles - GPT-4: %, Qwen: %, Claude: %', role_gpt4_id, role_qwen_id, role_claude_id;

    -- ============================================
    -- Create system users (one per model)
    -- ============================================

    RAISE NOTICE 'Creating system users in RuGPT organization';

    -- System user for GPT-4
    INSERT INTO users (
        org_id,
        name,
        username,
        email,
        password_hash,
        role_id,
        is_admin,
        is_system,
        is_active
    )
    VALUES (
        rugpt_org_id,
        'AI GPT-4',
        'ai_gpt4',
        'ai-gpt4@rugpt.system',
        NULL,  -- No password (system users can't login)
        role_gpt4_id,
        false,
        true,
        true
    )
    ON CONFLICT (email) DO UPDATE
    SET
        name = EXCLUDED.name,
        username = EXCLUDED.username,
        role_id = EXCLUDED.role_id,
        is_system = EXCLUDED.is_system,
        updated_at = NOW();

    -- System user for Qwen
    INSERT INTO users (
        org_id,
        name,
        username,
        email,
        password_hash,
        role_id,
        is_admin,
        is_system,
        is_active
    )
    VALUES (
        rugpt_org_id,
        'AI Qwen',
        'ai_qwen',
        'ai-qwen@rugpt.system',
        NULL,
        role_qwen_id,
        false,
        true,
        true
    )
    ON CONFLICT (email) DO UPDATE
    SET
        name = EXCLUDED.name,
        username = EXCLUDED.username,
        role_id = EXCLUDED.role_id,
        is_system = EXCLUDED.is_system,
        updated_at = NOW();

    -- System user for Claude
    INSERT INTO users (
        org_id,
        name,
        username,
        email,
        password_hash,
        role_id,
        is_admin,
        is_system,
        is_active
    )
    VALUES (
        rugpt_org_id,
        'AI Claude',
        'ai_claude',
        'ai-claude@rugpt.system',
        NULL,
        role_claude_id,
        false,
        true,
        true
    )
    ON CONFLICT (email) DO UPDATE
    SET
        name = EXCLUDED.name,
        username = EXCLUDED.username,
        role_id = EXCLUDED.role_id,
        is_system = EXCLUDED.is_system,
        updated_at = NOW();

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
