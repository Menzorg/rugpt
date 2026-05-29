-- Migration 045: invoice_clerk role + system user + scheduler-idempotency column.
--
-- 1) New AI role 'invoice_clerk' in system org 00000000-0000-0000-0000-000000000000.
--    agent_type 'simple' (single ReAct agent with tools — same as PM and every
--    other system-org role; supervisor is for multiagent subagent orchestration,
--    which invoice_clerk does not use). Tools: list_invoices/get_invoice/
--    show_modal. allowed_action_types whitelists three invoice action types from
--    Plan 2.
-- 2) System user 'invoice_clerk' bound to that role — visible as PM-like agent.
-- 3) invoices.last_notified_for_date DATE — prevents the scheduler from sending
--    duplicate reminders for the same day.

-- ============================================
-- 1. invoice_clerk role + system user in RuGPT system org
-- ============================================
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    clerk_role_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, agent_config, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Счетовод',
        'invoice_clerk',
        'AI-помощник по работе со счетами: показывает счета, предлагает решения для подтверждения через модалки',
        '',
        'google/gemma-4-31B-it',
        'simple',
        '{"allowed_action_types": ["invoice_approve", "invoice_reject", "invoice_mark_processed"]}'::jsonb,
        '["list_invoices", "get_invoice", "show_modal"]'::jsonb,
        'invoice_clerk.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING
    RETURNING id INTO clerk_role_id;

    IF clerk_role_id IS NULL THEN
        SELECT id INTO clerk_role_id FROM roles
        WHERE org_id = rugpt_org_id AND code = 'invoice_clerk';
    END IF;

    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    )
    VALUES (
        rugpt_org_id, 'Счетовод', 'invoice_clerk', 'invoice_clerk@rugpt.system', NULL,
        clerk_role_id, false, true, true
    )
    ON CONFLICT (email) DO NOTHING;
END $$;

-- ============================================
-- 2. scheduler-idempotency column
-- ============================================
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS last_notified_for_date DATE;

COMMENT ON COLUMN invoices.last_notified_for_date IS 'Last date a due-reminder was sent for this invoice. Prevents duplicate scheduler notifications same day.';
