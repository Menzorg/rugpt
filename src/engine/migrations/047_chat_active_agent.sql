-- Add active_agent column to chats table
-- Stores the currently active system user (AI agent) for the chat
-- Rename as_subagent_description to agent_scope_description on roles table

ALTER TABLE chats
    ADD COLUMN IF NOT EXISTS active_agent UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_chats_active_agent ON chats(active_agent) WHERE active_agent IS NOT NULL;

ALTER TABLE roles
    RENAME COLUMN as_subagent_description TO agent_scope_description;
