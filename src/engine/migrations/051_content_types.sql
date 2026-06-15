-- Migration 051: content types (file business categories) + file comment/content_type
-- + org-level policy whether free-form comments are allowed.
--
-- content_types — admin-managed per-org catalog ("Отчёт", "Заказ", ...), each with
--   a name + description. The description pre-fills a file's comment when the type
--   is chosen at upload.
-- user_files.comment        — human description of the file.
-- user_files.content_type_id — chosen content type (NULL = "Вручную"/free text).
--   Invariant (enforced server-side): content_type_id set => comment == type.description.
-- organizations.file_manual_comment_allowed — true: free text ("Вручную") allowed;
--   false: a real content type is mandatory at upload (comment = its description).

CREATE TABLE content_types (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unique active name per org (case-insensitive). Soft-deleted rows don't collide.
CREATE UNIQUE INDEX idx_content_types_org_name_active
    ON content_types(org_id, lower(name))
    WHERE is_active = true;

CREATE INDEX idx_content_types_org_active
    ON content_types(org_id)
    WHERE is_active = true;

ALTER TABLE user_files
    ADD COLUMN comment TEXT,
    ADD COLUMN content_type_id UUID REFERENCES content_types(id) ON DELETE SET NULL;

CREATE INDEX idx_user_files_content_type
    ON user_files(content_type_id)
    WHERE content_type_id IS NOT NULL;

ALTER TABLE organizations
    ADD COLUMN file_manual_comment_allowed BOOLEAN NOT NULL DEFAULT true;
