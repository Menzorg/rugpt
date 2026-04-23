-- Migration 025: Add is_active to correction_rules
ALTER TABLE correction_rules
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_correction_rules_active
    ON correction_rules(role_id, is_active)
    WHERE is_active = true;
