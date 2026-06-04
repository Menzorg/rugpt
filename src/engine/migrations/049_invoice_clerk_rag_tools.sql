-- Migration 049: grant rag_search + table_rows_search + expand_chunk to the invoice_clerk role.
--
-- The invoice_clerk role (system org 00000000-0000-0000-0000-000000000000) previously
-- had only list_invoices/get_invoice/show_modal.
-- Idempotent via JSONB containment check (@>): appends only the missing tools.

UPDATE roles
SET tools = tools || '["rag_search", "table_rows_search", "expand_chunk"]'::jsonb,
    updated_at = NOW()
WHERE org_id = '00000000-0000-0000-0000-000000000000'::uuid
  AND code = 'invoice_clerk'
  AND NOT (tools @> '["rag_search", "table_rows_search", "expand_chunk"]'::jsonb);
