-- Migration 006: Task Polls
-- Morning polls for employees to report task status

CREATE TABLE IF NOT EXISTS task_polls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    poll_date DATE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    responses JSONB DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE
);

-- Один опрос на сотрудника в день
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_polls_unique_daily
    ON task_polls(assignee_user_id, poll_date);

-- Поиск опросов сотрудника
CREATE INDEX IF NOT EXISTS idx_task_polls_assignee
    ON task_polls(assignee_user_id, poll_date DESC);

-- Pending опросы для scheduler (expire check)
CREATE INDEX IF NOT EXISTS idx_task_polls_pending
    ON task_polls(status, expires_at)
    WHERE status = 'pending';

-- Опросы по организации и дате (для вечернего отчёта)
CREATE INDEX IF NOT EXISTS idx_task_polls_org_date
    ON task_polls(org_id, poll_date);

COMMENT ON TABLE task_polls IS 'Daily morning polls for employees. One per employee per day.';
COMMENT ON COLUMN task_polls.status IS 'pending | completed | expired';
COMMENT ON COLUMN task_polls.responses IS 'JSON array: [{task_id, new_status, comment}]';
