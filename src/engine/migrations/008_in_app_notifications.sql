-- Migration 008: In-App Notifications
-- Bell icon notifications for tasks, polls, reports, mentions

CREATE TABLE IF NOT EXISTS in_app_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    type VARCHAR(30) NOT NULL,
    title VARCHAR(500) NOT NULL,
    content TEXT,
    reference_type VARCHAR(30),
    reference_id UUID,
    is_read BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Основной индекс: уведомления пользователя (непрочитанные первыми)
CREATE INDEX IF NOT EXISTS idx_in_app_notif_user
    ON in_app_notifications(user_id, is_read, created_at DESC);

-- Быстрый count непрочитанных
CREATE INDEX IF NOT EXISTS idx_in_app_notif_unread
    ON in_app_notifications(user_id)
    WHERE is_read = false;

COMMENT ON TABLE in_app_notifications IS 'In-app bell notifications. Separate from external notification channels (telegram/email).';
COMMENT ON COLUMN in_app_notifications.type IS 'new_task | poll | report | mention | task_status_change | system';
COMMENT ON COLUMN in_app_notifications.reference_type IS 'task | task_poll | task_report | message | null';
COMMENT ON COLUMN in_app_notifications.reference_id IS 'ID of the related entity';
