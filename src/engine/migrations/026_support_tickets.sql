-- Migration 023: support tickets (tech support chat)
--
-- Creates RuGPT Support org, support_assistant role + system user,
-- extends ChatType with 'support', creates support_tickets and
-- support_ticket_events tables.
--
-- Idempotent via ON CONFLICT and IF NOT EXISTS — safe to re-run.

-- 1. RuGPT Support organization
INSERT INTO organizations (id, name, slug, description) VALUES (
  '00000001-0000-0000-0000-000000000000',
  'RuGPT Support',
  'rugpt-support',
  'Команда тех. поддержки RuGPT'
) ON CONFLICT (id) DO NOTHING;

-- 2. Support assistant role + system user support_ai (in RuGPT system org)
-- PL/pgSQL DO-block (pattern from 003_system_user.sql) for cross-row id linking.
DO $$
DECLARE
    role_support_ai_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    ) VALUES (
        '00000000-0000-0000-0000-000000000000',
        'Support Assistant',
        'support_assistant',
        'AI первая линия тех. поддержки RuGPT',
        'Вы — AI-ассистент тех. поддержки RuGPT. Отвечаете кратко по вопросам использования продукта.',
        'hosted_vllm/google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'support_assistant.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;

    -- Resolve id whether row was inserted or already existed
    SELECT id INTO role_support_ai_id FROM roles
    WHERE org_id = '00000000-0000-0000-0000-000000000000' AND code = 'support_assistant';

    INSERT INTO users (
        org_id, username, name, email, is_system, role_id, is_active
    ) VALUES (
        '00000000-0000-0000-0000-000000000000',
        'support_ai',
        'AI Support',
        'ai-support@rugpt.system',
        true,
        role_support_ai_id,
        true
    )
    ON CONFLICT (org_id, username) DO UPDATE SET role_id = EXCLUDED.role_id;
END $$;

-- 3. Extend ChatType to include 'support'
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_type_check;
ALTER TABLE chats ADD CONSTRAINT chats_type_check
  CHECK (type IN ('direct', 'task', 'project', 'support'));

ALTER TABLE chats ADD COLUMN IF NOT EXISTS support_ticket_id UUID;
CREATE INDEX IF NOT EXISTS idx_chats_support_ticket ON chats(support_ticket_id)
  WHERE support_ticket_id IS NOT NULL;

-- 4. support_tickets table
CREATE TABLE IF NOT EXISTS support_tickets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  requester_user_id UUID NOT NULL REFERENCES users(id),
  requester_org_id  UUID NOT NULL REFERENCES organizations(id),

  category VARCHAR(20) NOT NULL CHECK (category IN ('how_to', 'bug', 'other')),
  status   VARCHAR(20) NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'in_progress', 'closed')),

  assignee_user_id UUID REFERENCES users(id),

  ai_handoff_at        TIMESTAMP,
  ai_first_response_at TIMESTAMP,

  closed_at         TIMESTAMP,
  closed_by_user_id UUID REFERENCES users(id),
  closed_by_role    VARCHAR(20) CHECK (closed_by_role IN ('requester', 'operator')),

  title VARCHAR(200),

  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_requester ON support_tickets(requester_user_id, status);
CREATE INDEX IF NOT EXISTS idx_support_tickets_assignee  ON support_tickets(assignee_user_id, status)
  WHERE assignee_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_support_tickets_queue     ON support_tickets(created_at)
  WHERE status = 'open' AND assignee_user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_support_tickets_closed    ON support_tickets(closed_at)
  WHERE status = 'closed';
CREATE INDEX IF NOT EXISTS idx_support_tickets_requester_org ON support_tickets(requester_org_id);

-- Add FK after both tables exist (idempotent: skip if constraint already present)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chats_support_ticket_id_fkey'
    ) THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_support_ticket_id_fkey
            FOREIGN KEY (support_ticket_id) REFERENCES support_tickets(id) ON DELETE SET NULL;
    END IF;
END $$;

-- 5. support_ticket_events audit trail
CREATE TABLE IF NOT EXISTS support_ticket_events (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id  UUID NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
  actor_user_id UUID NOT NULL REFERENCES users(id),
  actor_role VARCHAR(20) NOT NULL
    CHECK (actor_role IN ('requester', 'operator', 'ai', 'system')),
  event_type VARCHAR(50) NOT NULL,
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Promote event_type to VARCHAR(50) if table was created with older width
ALTER TABLE support_ticket_events ALTER COLUMN event_type TYPE VARCHAR(50);

CREATE INDEX IF NOT EXISTS idx_support_ticket_events_ticket
  ON support_ticket_events(ticket_id, created_at DESC);
