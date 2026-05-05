-- Migration 036: chat_read_state
-- High-water-mark per (chat, user) для подсчёта unread сообщений.

CREATE TABLE IF NOT EXISTS chat_read_state (
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    last_read_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_read_state_user ON chat_read_state(user_id);

COMMENT ON TABLE chat_read_state IS
    'High-water-mark per (chat, user) for unread message counting. UPSERT idempotent via PK. last_read_message_id is reference-only; counting uses last_read_at against messages(chat_id, created_at).';
