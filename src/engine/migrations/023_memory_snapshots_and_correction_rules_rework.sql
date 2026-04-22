-- Migration 023: Memory snapshots, correction_rules rework, mem_id on chats/messages

-- ============================================
-- memory_snapshots
-- ============================================
CREATE TABLE IF NOT EXISTS memory_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS update_memory_snapshots_updated_at ON memory_snapshots;
CREATE TRIGGER update_memory_snapshots_updated_at
    BEFORE UPDATE ON memory_snapshots
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- correction_rules rework
-- Drop old table and recreate with new schema
-- ============================================
DROP TABLE IF EXISTS correction_rules CASCADE;

CREATE TABLE correction_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL,
    mem_embedding VECTOR(1024),
    src_user_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    user_message_embedding VECTOR(1024),
    src_ai_response_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    user_correction_text TEXT,
    extracted_lesson TEXT
);

CREATE INDEX idx_correction_rules_role ON correction_rules(role_id);
CREATE INDEX idx_correction_rules_mem ON correction_rules(mem_id) WHERE mem_id IS NOT NULL;

-- ============================================
-- mem_id on chats
-- ============================================
ALTER TABLE chats ADD COLUMN IF NOT EXISTS mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_chats_mem ON chats(mem_id) WHERE mem_id IS NOT NULL;

-- ============================================
-- mem_id on messages (filled by trigger from chat)
-- ============================================
ALTER TABLE messages ADD COLUMN IF NOT EXISTS mem_id UUID REFERENCES memory_snapshots(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_messages_mem ON messages(mem_id) WHERE mem_id IS NOT NULL;

-- Trigger: on INSERT into messages, copy mem_id from the parent chat
CREATE OR REPLACE FUNCTION fill_message_mem_id()
RETURNS TRIGGER AS $$
BEGIN
    SELECT mem_id INTO NEW.mem_id FROM chats WHERE id = NEW.chat_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_fill_mem_id ON messages;
CREATE TRIGGER trg_messages_fill_mem_id
    BEFORE INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION fill_message_mem_id();
