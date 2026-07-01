-- Migration 060: overdue becomes an overlay flag instead of a status.
--
-- Previously the scheduler overwrote a task's work status with 'overdue', which
-- froze the assignee out of take/mark_done. Now `is_overdue` is a separate flag,
-- the work status (created/in_progress/awaiting_review/done) is preserved, and
-- only managers may shift the deadline.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_overdue BOOLEAN NOT NULL DEFAULT false;

-- Migrate existing status='overdue' rows: set the flag and restore the work
-- status the task had before going overdue. The old scheduler recorded that in
-- the 'overdue' task_event payload under `from_status`; fall back to in_progress.
UPDATE tasks t
SET is_overdue = true,
    status = COALESCE(
        (SELECT te.payload->>'from_status'
           FROM task_events te
          WHERE te.task_id = t.id AND te.event_type = 'overdue'
          ORDER BY te.created_at DESC
          LIMIT 1),
        'in_progress'
    ),
    updated_at = NOW()
WHERE t.status = 'overdue';

-- Safety net: any restored value that isn't a valid work status → in_progress.
UPDATE tasks
SET status = 'in_progress'
WHERE status NOT IN ('created', 'in_progress', 'awaiting_review', 'done');

CREATE INDEX IF NOT EXISTS idx_tasks_is_overdue ON tasks(is_overdue) WHERE is_overdue;
