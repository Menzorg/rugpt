-- Migration 016: Task ownership and deadline negotiation
ALTER TABLE tasks
    ADD COLUMN created_by_user_id UUID REFERENCES users(id),
    ADD COLUMN awaiting_review_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline TIMESTAMP WITH TIME ZONE,
    ADD COLUMN proposed_deadline_by UUID REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_tasks_created_by
    ON tasks(created_by_user_id)
    WHERE is_active = true;

COMMENT ON COLUMN tasks.created_by_user_id IS 'User who created the task (for priority calculation and ownership)';
COMMENT ON COLUMN tasks.awaiting_review_at IS 'Set when assignee moves task to awaiting_review; NULL otherwise';
COMMENT ON COLUMN tasks.proposed_deadline IS 'Alternative deadline proposed by assignee; NULL if no pending proposal';
COMMENT ON COLUMN tasks.proposed_deadline_by IS 'User who proposed the alternative deadline';
