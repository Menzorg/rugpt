-- Migration 032: Task participants (M:N)
-- Дополнительные участники задачи (chat collaborators).
-- Отдельная сущность от assignee (один) и creator (один).

CREATE TABLE IF NOT EXISTS task_participants (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    added_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    added_by_user_id UUID REFERENCES users(id),
    PRIMARY KEY (task_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_task_participants_user
    ON task_participants(user_id);

COMMENT ON TABLE task_participants IS
    'Additional task participants (chat collaborators). Separate from assignee (one) and creator. Idempotent inserts via PK; ON DELETE CASCADE on task removal.';
