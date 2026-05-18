-- Migration 042: add ON DELETE CASCADE to all FKs referencing organizations(id).
--
-- Early tables (001-002) already had CASCADE on org_id (users, roles, chats,
-- calendar_events, correction_rules). Later additions (departments, tasks,
-- in_app_notifications, user_files, projects, support_tickets, user_file_folders,
-- etc.) shipped without it — accidental drift, not architectural intent.
-- This migration makes the FK convention consistent: deleting an organization
-- nukes the whole tenant in one stroke.

ALTER TABLE departments
    DROP CONSTRAINT departments_org_id_fkey,
    ADD CONSTRAINT departments_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE department_visibility
    DROP CONSTRAINT department_visibility_org_id_fkey,
    ADD CONSTRAINT department_visibility_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE tasks
    DROP CONSTRAINT tasks_org_id_fkey,
    ADD CONSTRAINT tasks_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE task_polls
    DROP CONSTRAINT task_polls_org_id_fkey,
    ADD CONSTRAINT task_polls_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE task_reports
    DROP CONSTRAINT task_reports_org_id_fkey,
    ADD CONSTRAINT task_reports_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE in_app_notifications
    DROP CONSTRAINT in_app_notifications_org_id_fkey,
    ADD CONSTRAINT in_app_notifications_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE user_files
    DROP CONSTRAINT user_files_org_id_fkey,
    ADD CONSTRAINT user_files_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE user_file_folders
    DROP CONSTRAINT user_file_folders_org_id_fkey,
    ADD CONSTRAINT user_file_folders_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

ALTER TABLE projects
    DROP CONSTRAINT projects_org_id_fkey,
    ADD CONSTRAINT projects_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE;

-- support_tickets uses non-standard column name `requester_org_id`.
-- Включаем в CASCADE: тикет неотделим от клиента, отдельный historic-retention
-- бессмыслен (тикет содержит customer-info). Если потребуется отдельная архивация
-- закрытых тикетов — выгружать перед удалением org, не на уровне FK.
ALTER TABLE support_tickets
    DROP CONSTRAINT support_tickets_requester_org_id_fkey,
    ADD CONSTRAINT support_tickets_requester_org_id_fkey
        FOREIGN KEY (requester_org_id) REFERENCES organizations(id) ON DELETE CASCADE;
