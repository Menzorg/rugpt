-- Migration 038: replace legacy document-listing tool names.
-- `list_private_documents` is now `list_own_documents`.
-- Older `list_documents` role configs are expanded to the explicit own/global pair.

UPDATE roles
SET tools = (tools - 'list_private_documents') || '["list_own_documents"]'::jsonb,
    updated_at = NOW()
WHERE tools @> '["list_private_documents"]'::jsonb
  AND NOT (tools @> '["list_own_documents"]'::jsonb);

UPDATE roles
SET tools = tools - 'list_private_documents',
    updated_at = NOW()
WHERE tools @> '["list_private_documents"]'::jsonb
  AND tools @> '["list_own_documents"]'::jsonb;

UPDATE roles
SET tools = (tools - 'list_documents' - 'list_own_documents' - 'list_global_documents')
        || '["list_own_documents", "list_global_documents"]'::jsonb,
    updated_at = NOW()
WHERE tools @> '["list_documents"]'::jsonb;
