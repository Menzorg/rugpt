-- Migration 050: grant analyze_image to the invoice_clerk role.
--
-- The invoice_clerk role (system org 00000000-0000-0000-0000-000000000000) gains
-- the analyze_image tool so it can extract details directly from uploaded images
-- (e.g. invoice scans) via a vision-capable LLM.
-- Idempotent via JSONB containment check (@>): appends only if missing.

UPDATE roles
SET tools = tools || '["analyze_image"]'::jsonb,
    updated_at = NOW()
WHERE org_id = '00000000-0000-0000-0000-000000000000'::uuid
  AND code = 'invoice_clerk'
  AND NOT (tools @> '["analyze_image"]'::jsonb);
