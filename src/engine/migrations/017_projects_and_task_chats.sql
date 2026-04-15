-- Migration 017: Projects, task chats, project chats, task events

-- ============================================
-- 1. Projects table
-- ============================================
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    description TEXT,
    created_by_user_id UUID REFERENCES users(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id) WHERE is_active = true;

COMMENT ON TABLE projects IS 'Project entity for grouping tasks. Names can be duplicated within an org.';

-- ============================================
-- 2. Link tasks to projects
-- ============================================
ALTER TABLE tasks
    ADD COLUMN project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id) WHERE is_active = true;

COMMENT ON COLUMN tasks.project_id IS 'Optional link to a project. ON DELETE SET NULL -- project deletion keeps task.';

-- ============================================
-- 3. Task events (audit trail)
-- ============================================
CREATE TABLE IF NOT EXISTS task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    actor_user_id UUID REFERENCES users(id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, created_at DESC);

COMMENT ON TABLE task_events IS 'Audit log of task changes. Rendered as History in UI. NOT the same as chat messages.';
COMMENT ON COLUMN task_events.event_type IS 'created|took|marked_done|accepted|rejected|deadline_set|deadline_proposed|deadline_proposal_accepted|deadline_proposal_rejected|assignee_changed|project_changed|cancelled|overdue';

-- NOTE: event_type strings 'cancelled' and 'rejected' are audit events, NOT task.status values.
-- tasks.status IN (created, in_progress, awaiting_review, done, overdue) (see models/task.py:VALID_STATUSES).
-- reject_task returns the task to status='in_progress' and writes a 'rejected' event.
-- deactivate (soft-delete) sets is_active=false and writes a 'cancelled' event; status is not changed.

-- ============================================
-- 4. Extend chats for task and project types
-- ============================================
-- chats.type is VARCHAR(20) without CHECK constraint -- new values 'task'/'project'
-- can be added without DDL. We just clean up legacy 'main'/'group' values
-- and add the new FK columns.

-- Convert legacy types to direct (preserves any messages, no data loss)
UPDATE chats SET type = 'direct' WHERE type IN ('main', 'group');

-- New default for clarity
ALTER TABLE chats ALTER COLUMN type SET DEFAULT 'direct';

-- New FK columns for task and project association.
-- Soft-delete policy: NO CASCADE. Deletion of a task or project is handled
-- at the service layer (archive_task_chat / archive_project_chat set is_active=false),
-- so chats are preserved as archive and never physically removed by FK propagation.
ALTER TABLE chats
    ADD COLUMN task_id UUID REFERENCES tasks(id),
    ADD COLUMN project_id UUID REFERENCES projects(id);

CREATE INDEX IF NOT EXISTS idx_chats_task ON chats(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chats_project ON chats(project_id) WHERE project_id IS NOT NULL;

COMMENT ON COLUMN chats.type IS 'direct | task | project';
COMMENT ON COLUMN chats.task_id IS 'For type=task: references the task this chat belongs to';
COMMENT ON COLUMN chats.project_id IS 'For type=project: references the project this chat belongs to';
