-- Migration 043: add metadata JSONB to messages.
-- Used by the modal protocol (messages.metadata.modal) and reusable for future
-- per-message extension fields.

ALTER TABLE messages
    ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
