-- 015_departments.sql
-- Departments, visibility rules, user department fields, org context

-- Departments (flat list, no nesting)
CREATE TABLE departments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_departments_org_id ON departments(org_id);

-- Symmetric visibility rules between departments
-- CHECK ensures department_a_id < department_b_id to prevent duplicate pairs
CREATE TABLE department_visibility (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    department_a_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    department_b_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(department_a_id, department_b_id),
    CHECK(department_a_id < department_b_id)
);
CREATE INDEX idx_dept_vis_a ON department_visibility(department_a_id);
CREATE INDEX idx_dept_vis_b ON department_visibility(department_b_id);

-- User belongs to one department, is_head = department manager
ALTER TABLE users ADD COLUMN department_id UUID REFERENCES departments(id) ON DELETE SET NULL;
ALTER TABLE users ADD COLUMN is_head BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_users_department_id ON users(department_id);

-- Org context text injected into all role prompts
ALTER TABLE organizations ADD COLUMN org_context TEXT NOT NULL DEFAULT '';
