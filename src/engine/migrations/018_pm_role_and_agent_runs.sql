-- Migration 018: PM agent role + system user + agent_runs idempotency table
-- Item 10 (PM-agent) + preparation for async agent execution via Kafka (item 5)

-- ============================================
-- 1. PM role + system user in RuGPT system org
-- ============================================
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    pm_role_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Проджект-менеджер',
        'pm',
        'AI-ассистент для управления задачами и уведомлений',
        'Вы — PM-агент, проджект-менеджер.',
        'qwen3:14b',
        'simple',
        '["task_create", "task_query", "task_update"]'::jsonb,
        'pm.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO pm_role_id;

    IF pm_role_id IS NULL THEN
        SELECT id INTO pm_role_id FROM roles WHERE org_id = rugpt_org_id AND code = 'pm';
    END IF;

    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'PM-агент', 'pm', 'pm@rugpt.system', NULL,
        pm_role_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;
END $$;

-- ============================================
-- 2. agent_runs table (idempotency for async agent execution)
-- ============================================
CREATE TABLE IF NOT EXISTS agent_runs (
    request_id UUID PRIMARY KEY,
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    triggering_user_id UUID REFERENCES users(id),
    role_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs(chat_id, created_at DESC);

COMMENT ON TABLE agent_runs IS 'Tracks async agent executions. Used for Kafka retry idempotency and audit. Item 10/5.';
COMMENT ON COLUMN agent_runs.status IS 'pending | running | done | failed';
