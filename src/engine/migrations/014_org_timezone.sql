-- Migration 014: Add timezone to organizations
-- IANA-идентификатор часового пояса организации для SchedulerService

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Moscow';
    -- Примеры: 'Europe/Moscow', 'Asia/Novosibirsk', 'UTC'
    -- SchedulerService использует это поле при расчёте времени срабатывания
    -- calendar событий для конкретной организации

COMMENT ON COLUMN organizations.timezone IS 'IANA timezone identifier used by SchedulerService for calendar events. Default: Europe/Moscow.';
