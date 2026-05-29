-- Migration 021: Attach new tools to PM and doc_search roles.
-- Idempotent via JSONB containment check (@>).

UPDATE roles
SET tools = tools || '["user_search"]'::jsonb,
    updated_at = NOW()
WHERE code = 'pm' AND NOT (tools @> '["user_search"]'::jsonb);

UPDATE roles
SET tools = tools || '["list_documents"]'::jsonb,
    updated_at = NOW()
WHERE code = 'doc_search' AND NOT (tools @> '["list_documents"]'::jsonb);
