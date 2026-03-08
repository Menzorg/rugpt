-- Migration 005: Task Management
-- Creates tasks table for employee task tracking

CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
    assignee_user_id UUID NOT NULL REFERENCES users(id),
    deadline TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Основной индекс: задачи сотрудника по статусу
CREATE INDEX IF NOT EXISTS idx_tasks_assignee
    ON tasks(assignee_user_id, status)
    WHERE is_active = true;

-- Задачи по организации
CREATE INDEX IF NOT EXISTS idx_tasks_org
    ON tasks(org_id)
    WHERE is_active = true;

-- Активные задачи для scheduler (проверка overdue)
CREATE INDEX IF NOT EXISTS idx_tasks_deadline
    ON tasks(deadline)
    WHERE is_active = true AND deadline IS NOT NULL AND status NOT IN ('done', 'overdue');

COMMENT ON TABLE tasks IS 'Employee tasks managed by AI roles. Created via chat (@@mention) or UI.';
COMMENT ON COLUMN tasks.status IS 'created | in_progress | done | overdue';
COMMENT ON COLUMN tasks.assignee_user_id IS 'Employee assigned to this task';

