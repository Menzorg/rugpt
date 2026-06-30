-- Migration 059: task merge feature.
--   1. Hidden system role `task_merger` (in the RuGPT system org, so it never
--      appears in /web role/user listings — invisible to the webclient).
--      It only FORMULATES the merged task (title/description); a deterministic
--      service performs create + close.
--   2. tasks.merged_into_task_id — points a closed source task at its target.
--   3. task_merge_requests — one row per merge preview. Stores an as-is snapshot
--      of the source tasks + their chat transcripts so a mistaken merge can be
--      reconstructed by hand.

-- 1. Hidden system role -------------------------------------------------------
DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000'::uuid;
BEGIN
    INSERT INTO roles (
        org_id, name, code, description, agent_scope_description,
        system_prompt, model_name, agent_type, tools, prompt_file, is_active
    )
    VALUES (
        rugpt_org_id,
        'Объединитель задач',
        'task_merger',
        'Внутренняя системная роль: объединяет несколько задач в одну, формулируя название и описание новой задачи по контексту исходных. Недоступна из вебклиента.',
        '',
        'Ты — агент объединения задач. По данным нескольких задач и их переписок сформулируй одну объединённую задачу в строгом JSON.',
        'google/gemma-4-31B-it',
        'simple',
        '[]'::jsonb,
        'task_merger.md',
        true
    )
    ON CONFLICT (org_id, code) DO NOTHING;
END $$;

-- 2. Link column for closed source tasks -------------------------------------
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS merged_into_task_id UUID REFERENCES tasks(id) ON DELETE SET NULL;

-- 3. Merge requests (preview + snapshot for recovery) -------------------------
CREATE TABLE IF NOT EXISTS task_merge_requests (
    id                   UUID PRIMARY KEY,
    org_id               UUID NOT NULL,
    actor_user_id        UUID NOT NULL,
    source_task_ids      UUID[] NOT NULL,
    source_snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_title       TEXT,
    proposed_description TEXT,
    proposed_summary     TEXT,
    status               VARCHAR(20) NOT NULL DEFAULT 'previewed',  -- previewed | applied | failed
    new_task_id          UUID REFERENCES tasks(id) ON DELETE SET NULL,
    error                TEXT,
    created_at           TIMESTAMP NOT NULL DEFAULT NOW(),
    applied_at           TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_merge_requests_org ON task_merge_requests(org_id);
CREATE INDEX IF NOT EXISTS idx_task_merge_requests_actor ON task_merge_requests(actor_user_id);
