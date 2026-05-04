-- =================================================================
-- 035: change user_files dedup constraint from (org_id, content_hash)
--      to (user_id, content_hash)
-- =================================================================
--
-- Контекст: оригинальный constraint из миграции 012 запрещал двум активным
-- user_files в одной орге иметь одинаковый content_hash. Цель была — не
-- дублировать ingest/RAG-индексацию для одного и того же контента.
--
-- Проблема: ломает фичу «В мои файлы» (file_service.clone). Если в орге
-- уже есть active user_file с этим контентом (у любого юзера), второй
-- юзер не может его клонировать к себе. UniqueViolationError при INSERT.
--
-- Решение: уникальность на (user_id, content_hash). Каждый юзер
-- уникален в своих active файлах. Дедупликация на уровне физического
-- хранения (storage_key) остаётся прежней — файлы с одинаковым контентом
-- в орге шарят storage_key (см. file_service.clone), один файл на диске.
-- =================================================================

DROP INDEX IF EXISTS idx_user_files_org_content_hash_active_uq;

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_files_user_content_hash_active_uq
    ON user_files(user_id, content_hash)
    WHERE is_active = true AND content_hash != '';
