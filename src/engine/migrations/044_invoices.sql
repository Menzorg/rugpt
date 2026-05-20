-- Migration 044: invoices table + accountant_user_id on organizations.
-- Invoice approval flow:
--   uploader (any active user) creates an invoice referencing an existing
--   user_files row (the binary). Admin approves or rejects. Once approved,
--   the designated accountant (organizations.accountant_user_id) marks the
--   invoice as processed. due_date is entered manually at upload.

CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    file_id UUID NOT NULL REFERENCES user_files(id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users(id),
    due_date DATE,
    status VARCHAR(20) NOT NULL DEFAULT 'created',
        -- created | approved | rejected | processed
    approved_by_user_id UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    rejection_reason TEXT,
    processed_at TIMESTAMPTZ,
    processed_by_user_id UUID REFERENCES users(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT invoices_status_chk CHECK (
        status IN ('created', 'approved', 'rejected', 'processed')
    )
);

CREATE INDEX idx_invoices_org_status
    ON invoices(org_id, status)
    WHERE is_active = true;

CREATE INDEX idx_invoices_uploader
    ON invoices(uploaded_by_user_id)
    WHERE is_active = true;

CREATE INDEX idx_invoices_due_date
    ON invoices(due_date)
    WHERE status = 'approved' AND processed_at IS NULL AND is_active = true;

ALTER TABLE organizations
    ADD COLUMN accountant_user_id UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX idx_orgs_accountant
    ON organizations(accountant_user_id)
    WHERE accountant_user_id IS NOT NULL;
