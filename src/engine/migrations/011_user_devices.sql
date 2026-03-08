-- 011: User devices for Zero Trust
-- Stores device public keys for request signature verification (ECDSA P-256)

CREATE TABLE IF NOT EXISTS user_devices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_public_key TEXT NOT NULL,
    device_name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT unique_user_device_key UNIQUE (user_id, device_public_key)
);

CREATE INDEX IF NOT EXISTS idx_user_devices_user_active
    ON user_devices(user_id) WHERE is_active = true;
