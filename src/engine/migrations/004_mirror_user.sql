-- Migration 004: Mirror system user
-- Adds "AI Помощник" (mirror) system user to RuGPT organization.
-- Mirror user has no fixed role — responds using the SENDER's assigned role.
-- Can be mentioned as @@mirror in any chat.

DO $$
DECLARE
    rugpt_org_id UUID := '00000000-0000-0000-0000-000000000000';
    mirror_user_id UUID;
BEGIN
    mirror_user_id := gen_random_uuid();

    INSERT INTO users (
        id, org_id, name, username, email, password_hash,
        role_id, is_admin, is_system, is_active,
        created_at, updated_at
    ) VALUES (
        mirror_user_id,
        rugpt_org_id,
        'AI Помощник',
        'mirror',
        'mirror@rugpt.system',
        NULL,
        NULL,       -- no role: uses sender's role
        false,
        true,       -- is_system = true
        true,
        NOW(), NOW()
    );

    RAISE NOTICE 'Created mirror system user: % (AI Помощник)', mirror_user_id;
END $$;
