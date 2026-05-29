-- Migration 029: poll chat type, poll-scoped chat, summary field, AI dialog roles.
-- Idempotent.

-- 1. Extend ChatType to include 'poll'
ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_type_check;
ALTER TABLE chats ADD CONSTRAINT chats_type_check
  CHECK (type IN ('direct', 'task', 'project', 'support', 'poll'));

-- 2. chats.poll_id (FK back to task_polls)
ALTER TABLE chats ADD COLUMN IF NOT EXISTS poll_id UUID;
CREATE INDEX IF NOT EXISTS idx_chats_poll
  ON chats(poll_id) WHERE poll_id IS NOT NULL;

-- 3. task_polls.summary (markdown produced by poll_summarizer)
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS summary TEXT NULL;

-- 4. task_polls.task_ids — snapshot of active task IDs at poll creation
ALTER TABLE task_polls ADD COLUMN IF NOT EXISTS task_ids JSONB
  NOT NULL DEFAULT '[]'::jsonb;

-- 5. FK chats.poll_id -> task_polls.id (after both tables exist)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chats_poll_id_fkey'
    ) THEN
        ALTER TABLE chats
            ADD CONSTRAINT chats_poll_id_fkey
            FOREIGN KEY (poll_id) REFERENCES task_polls(id) ON DELETE CASCADE;
    END IF;
END $$;

-- 6. Roles: poll_interviewer + poll_summarizer in system org
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
    role_interviewer_id UUID;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    ) VALUES (
        rugpt_org_id,
        'AI-интервьюер опроса',
        'poll_interviewer',
        'AI-интервьюер для утреннего опроса сотрудников',
        'Вы — AI-интервьюер. Выясните у сотрудника статус каждой его задачи.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'poll_interviewer.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;

    SELECT id INTO role_interviewer_id FROM roles
    WHERE org_id = rugpt_org_id AND code = 'poll_interviewer';

    INSERT INTO roles (
        org_id, name, code, description, system_prompt, model_name,
        agent_type, tools, prompt_file, is_active
    ) VALUES (
        rugpt_org_id,
        'AI-сводчик опроса',
        'poll_summarizer',
        'AI-сводчик: формирует markdown-сводку из транскрипта опроса',
        'Вы — AI-сводчик. Извлеките сводку из транскрипта диалога.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'poll_summarizer.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;

    -- 7. System user for poll_interviewer (member of poll chats)
    INSERT INTO users (
        org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active
    ) VALUES (
        rugpt_org_id, 'AI-интервьюер', 'poll_interviewer_ai',
        'poll-interviewer@rugpt.system', NULL,
        role_interviewer_id, false, true, true
    )
    ON CONFLICT (org_id, username) DO UPDATE SET role_id = EXCLUDED.role_id;
END $$;
