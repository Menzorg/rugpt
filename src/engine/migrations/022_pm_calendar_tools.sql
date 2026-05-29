-- Migration 022: Attach calendar tools to PM role.
-- PM is a natural owner of scheduling/reminders — its command surface
-- already covers deadlines, so calendar fits the same mental model.
-- Idempotent via JSONB containment check (@>).

UPDATE roles
SET tools = tools || '["calendar_create", "calendar_query"]'::jsonb,
    updated_at = NOW()
WHERE code = 'pm' AND NOT (tools @> '["calendar_create"]'::jsonb);
