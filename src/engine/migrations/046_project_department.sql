-- 046_project_department.sql
-- Adds projects.department_id (owning department, frozen at creation = creator's
-- department). Backfills existing rows from the creator's current department.
-- Idempotent.

ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS department_id UUID NULL
  REFERENCES departments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_projects_department
  ON projects(department_id) WHERE department_id IS NOT NULL;

UPDATE projects p
   SET department_id = u.department_id
  FROM users u
 WHERE u.id = p.created_by_user_id
   AND p.department_id IS NULL;
