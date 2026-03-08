-- Migration 007: Task Reports
-- Evening AI-generated reports for managers

CREATE TABLE IF NOT EXISTS task_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    generated_for_user_id UUID NOT NULL REFERENCES users(id),
    report_date DATE NOT NULL,
    content TEXT NOT NULL,
    task_summaries JSONB DEFAULT '[]',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Отчёты руководителя по дате
CREATE INDEX IF NOT EXISTS idx_task_reports_user
    ON task_reports(generated_for_user_id, report_date DESC);

-- Отчёты организации
CREATE INDEX IF NOT EXISTS idx_task_reports_org
    ON task_reports(org_id, report_date DESC);

COMMENT ON TABLE task_reports IS 'AI-generated evening reports for managers. Aggregates task poll responses.';
COMMENT ON COLUMN task_reports.generated_for_user_id IS 'Manager who receives this report';
COMMENT ON COLUMN task_reports.content IS 'AI-generated human-readable report text';
COMMENT ON COLUMN task_reports.task_summaries IS 'Structured data: [{task_id, title, assignee_user_id, assignee_name, old_status, new_status, employee_comment, poll_completed}]';
