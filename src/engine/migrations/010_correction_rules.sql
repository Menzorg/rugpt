-- Migration 010: Correction Rules + ai_is_valid rename
-- Renames ai_validated → ai_is_valid (BOOLEAN DEFAULT NULL)
-- NULL = not reviewed, true = approved, false = rejected
-- Creates correction_rules table for role learning from user feedback

-- ============================================
-- Rename ai_validated → ai_is_valid, make nullable
-- ============================================
ALTER TABLE messages RENAME COLUMN ai_validated TO ai_is_valid;
ALTER TABLE messages ALTER COLUMN ai_is_valid DROP NOT NULL;
ALTER TABLE messages ALTER COLUMN ai_is_valid SET DEFAULT NULL;

-- User messages → true (auto-valid), AI messages with old false → NULL (pending)
UPDATE messages SET ai_is_valid = true WHERE sender_type = 'user';
UPDATE messages SET ai_is_valid = NULL WHERE sender_type = 'ai_role' AND ai_is_valid = false;

-- Recreate index for pending review (NULL = not yet reviewed)
DROP INDEX IF EXISTS idx_messages_unvalidated;
CREATE INDEX idx_messages_pending_review ON messages(sender_id)
    WHERE sender_type = 'ai_role' AND ai_is_valid IS NULL AND is_deleted = false;

-- ============================================
-- Correction Rules
-- ============================================
CREATE TABLE IF NOT EXISTS correction_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    original_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ai_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_question TEXT NOT NULL,
    ai_answer TEXT NOT NULL,
    correction_text TEXT NOT NULL,
    rule_text TEXT,
    created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_correction_rules_role ON correction_rules(role_id);
CREATE INDEX idx_correction_rules_org ON correction_rules(org_id);
CREATE INDEX idx_correction_rules_active ON correction_rules(role_id, is_active)
    WHERE is_active = true;

-- Apply updated_at trigger
DROP TRIGGER IF EXISTS update_correction_rules_updated_at ON correction_rules;
CREATE TRIGGER update_correction_rules_updated_at
    BEFORE UPDATE ON correction_rules
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
