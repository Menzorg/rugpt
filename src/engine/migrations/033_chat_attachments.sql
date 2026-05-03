-- 031_chat_attachments.sql
-- Adds: cloned_from_file_id on user_files, message_attachments junction.
-- Idempotent.

ALTER TABLE user_files
  ADD COLUMN IF NOT EXISTS cloned_from_file_id UUID NULL
  REFERENCES user_files(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_user_files_cloned_from
  ON user_files(cloned_from_file_id) WHERE cloned_from_file_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS message_attachments (
  message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  file_id    UUID NOT NULL REFERENCES user_files(id) ON DELETE CASCADE,
  position   SMALLINT NOT NULL DEFAULT 0,
  PRIMARY KEY (message_id, file_id)
);

CREATE INDEX IF NOT EXISTS idx_message_attachments_file
  ON message_attachments(file_id);

-- Redundant: PK btree on (message_id, file_id) already serves WHERE message_id=X
-- via leftmost-prefix matching. Drop if previously created.
DROP INDEX IF EXISTS idx_message_attachments_message;

COMMENT ON TABLE message_attachments IS 'Junction: messages ↔ user_files. Position controls display order. ON DELETE CASCADE on both sides — when message or file is hard-deleted, the link disappears (soft-delete via is_active is the normal path).';
COMMENT ON COLUMN user_files.cloned_from_file_id IS 'If non-null, this row is a metadata-only clone created via "Add to my files". storage_key matches the source — physical file is shared.';
