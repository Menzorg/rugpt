-- Migration 009: User Files for RAG
-- File metadata in PostgreSQL, binary data via StorageAdapter (local FS or S3)
-- Files belong to users; user's AI role accesses all user's files

CREATE TABLE IF NOT EXISTS user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
        -- сотрудник, которому принадлежит файл
    org_id UUID NOT NULL REFERENCES organizations(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
        -- руководитель, загрузивший файл
    storage_key VARCHAR(500) NOT NULL,
        -- ключ в хранилище: {org_id}/{user_id}/{file_id}.{ext}
        -- для local FS: путь относительно base_dir
        -- для S3: object key в бакете
    original_filename VARCHAR(500) NOT NULL,
    file_type VARCHAR(20) NOT NULL,
        -- pdf | docx
    file_size BIGINT NOT NULL,
    rag_status VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | indexing | indexed | failed
    rag_error TEXT,
    indexed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Файлы пользователя (для RAG-фильтрации)
CREATE INDEX IF NOT EXISTS idx_user_files_user
    ON user_files(user_id)
    WHERE is_active = true;

-- Файлы организации
CREATE INDEX IF NOT EXISTS idx_user_files_org
    ON user_files(org_id);

-- Pending файлы для индексации
CREATE INDEX IF NOT EXISTS idx_user_files_pending_rag
    ON user_files(rag_status)
    WHERE rag_status IN ('pending', 'indexing');

COMMENT ON TABLE user_files IS 'File metadata. Binary data stored via StorageAdapter (local FS or S3). Key format: {org_id}/{user_id}/{file_id}.{ext}';
COMMENT ON COLUMN user_files.user_id IS 'Employee who owns the file (role accesses these)';
COMMENT ON COLUMN user_files.uploaded_by_user_id IS 'Manager who uploaded the file';
COMMENT ON COLUMN user_files.storage_key IS 'Storage key: {org_id}/{user_id}/{file_id}.{ext}. Same format for local FS and S3.';
COMMENT ON COLUMN user_files.rag_status IS 'pending | indexing | indexed | failed';
COMMENT ON COLUMN user_files.file_type IS 'pdf | docx';
