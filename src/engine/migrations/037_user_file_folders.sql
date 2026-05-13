-- =================================================================
-- 037: user_file_folders + user_files.folder_id
-- =================================================================
-- Личные папки пользователя для организации файлов.
-- Adjacency list (parent_folder_id), soft-delete через is_active.
-- Уникальность имени в одном parent (case-insensitive) — partial unique index
-- с NULLS NOT DISTINCT (PG 15+).
-- =================================================================

CREATE TABLE IF NOT EXISTS user_file_folders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    org_id UUID NOT NULL REFERENCES organizations(id),
    parent_folder_id UUID REFERENCES user_file_folders(id),
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_folders_user_parent
    ON user_file_folders(user_id, parent_folder_id)
    WHERE is_active = true;

CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_name_unique
    ON user_file_folders(user_id, parent_folder_id, lower(name))
    NULLS NOT DISTINCT
    WHERE is_active = true;

ALTER TABLE user_files
    ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES user_file_folders(id);

CREATE INDEX IF NOT EXISTS idx_user_files_folder
    ON user_files(user_id, folder_id)
    WHERE is_active = true;

COMMENT ON TABLE user_file_folders IS
    'Personal folders for user files. Adjacency list, soft-delete via is_active.';
COMMENT ON COLUMN user_file_folders.parent_folder_id IS
    'NULL = root level. NOT ON DELETE CASCADE — soft-delete only.';
COMMENT ON COLUMN user_files.folder_id IS
    'NULL = root level. folder.user_id must equal file.user_id (service-enforced).';
