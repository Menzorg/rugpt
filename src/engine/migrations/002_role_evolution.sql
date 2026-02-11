-- RuGPT Phase 1: Role Evolution
-- Adds agent system columns to roles, creates calendar_events,
-- notification_channels, and notification_log tables.

-- ============================================
-- Roles: add agent system columns
-- ============================================
ALTER TABLE roles
  ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'simple',
  ADD COLUMN IF NOT EXISTS agent_config JSONB NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS tools JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS prompt_file VARCHAR(255);

-- prompt_file: path relative to prompts dir, e.g. "lawyer.md"
-- system_prompt remains for backward compatibility (fallback)

-- ============================================
-- Calendar Events
-- ============================================
CREATE TABLE IF NOT EXISTS calendar_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    event_type VARCHAR(20) NOT NULL DEFAULT 'one_time',  -- 'one_time', 'recurring'

    -- Scheduling
    scheduled_at TIMESTAMP WITH TIME ZONE,               -- for one_time events
    cron_expression VARCHAR(100),                         -- for recurring, e.g. "0 10 * * 4"
    next_trigger_at TIMESTAMP WITH TIME ZONE,             -- precomputed next trigger time
    last_triggered_at TIMESTAMP WITH TIME ZONE,
    trigger_count INTEGER NOT NULL DEFAULT 0,

    -- Source context
    source_chat_id UUID REFERENCES chats(id) ON DELETE SET NULL,
    source_message_id UUID REFERENCES messages(id) ON DELETE SET NULL,

    -- Metadata
    metadata JSONB NOT NULL DEFAULT '{}',                 -- reminder text, params
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,

    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_role ON calendar_events(role_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_org ON calendar_events(org_id);
CREATE INDEX IF NOT EXISTS idx_calendar_events_next_trigger
    ON calendar_events(next_trigger_at)
    WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_calendar_events_active
    ON calendar_events(is_active) WHERE is_active = true;

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_calendar_events_updated_at ON calendar_events;
CREATE TRIGGER update_calendar_events_updated_at
    BEFORE UPDATE ON calendar_events
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Notification Channels
-- ============================================
CREATE TABLE IF NOT EXISTS notification_channels (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    channel_type VARCHAR(20) NOT NULL,                    -- 'telegram', 'email', 'chat'
    config JSONB NOT NULL DEFAULT '{}',                   -- {chat_id: "..."} or {email: "..."}
    is_enabled BOOLEAN NOT NULL DEFAULT true,
    is_verified BOOLEAN NOT NULL DEFAULT false,
    priority INTEGER NOT NULL DEFAULT 0,                  -- higher = tried first

    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    UNIQUE(user_id, channel_type)
);

CREATE INDEX IF NOT EXISTS idx_notification_channels_user ON notification_channels(user_id);
CREATE INDEX IF NOT EXISTS idx_notification_channels_org ON notification_channels(org_id);

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_notification_channels_updated_at ON notification_channels;
CREATE TRIGGER update_notification_channels_updated_at
    BEFORE UPDATE ON notification_channels
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Notification Log
-- ============================================
CREATE TABLE IF NOT EXISTS notification_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_type VARCHAR(20) NOT NULL,
    event_id UUID REFERENCES calendar_events(id) ON DELETE SET NULL,
    role_id UUID REFERENCES roles(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',        -- 'pending', 'sent', 'failed'
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,

    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notification_log_user ON notification_log(user_id);
CREATE INDEX IF NOT EXISTS idx_notification_log_event ON notification_log(event_id);
CREATE INDEX IF NOT EXISTS idx_notification_log_status ON notification_log(status);

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_notification_log_updated_at ON notification_log;
CREATE TRIGGER update_notification_log_updated_at
    BEFORE UPDATE ON notification_log
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Update existing roles: set prompt_file for known roles
-- ============================================
UPDATE roles SET prompt_file = 'lawyer.md' WHERE code = 'lawyer';
UPDATE roles SET prompt_file = 'accountant.md' WHERE code = 'accountant';
UPDATE roles SET prompt_file = 'hr.md' WHERE code = 'hr';
UPDATE roles SET prompt_file = 'chu.md' WHERE code = 'chu';
