-- Migration 030: task priority as a stored, editable field.
-- Previously priority was computed at read time from the creator's role
-- (admin=3, head=2, regular=1). Promote it to a real column so it can be
-- edited and so list queries don't need the JOIN+CASE.
-- Idempotent.

-- 1. Column with default 1 (regular). Adding NOT NULL with default is safe.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 1;

-- 2. Range check 1..3. Drop-and-add so re-running normalizes the constraint name.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_priority_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_priority_check
  CHECK (priority BETWEEN 1 AND 3);

-- 3. Backfill from creator's role. Only run while every row still holds the
-- default 1 — otherwise a re-run would clobber priorities edited later.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tasks WHERE priority <> 1) THEN
        UPDATE tasks t
        SET priority = CASE
            WHEN u.is_admin THEN 3
            WHEN u.is_head THEN 2
            ELSE 1
        END
        FROM users u
        WHERE u.id = t.created_by_user_id;
    END IF;
END $$;

-- 4. Index supporting list_by_assignee_with_priority's ORDER BY.
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_priority
  ON tasks(assignee_user_id, priority DESC, deadline)
  WHERE is_active = true;
