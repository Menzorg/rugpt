-- RuGPT Initial Migration
-- Creates tables for organizations, users, roles, chats, and messages

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- Organizations
-- ============================================
CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_organizations_slug ON organizations(slug);
CREATE INDEX IF NOT EXISTS idx_organizations_active ON organizations(is_active) WHERE is_active = true;

-- ============================================
-- Roles (AI Agents)
-- ============================================
CREATE TABLE IF NOT EXISTS roles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    code VARCHAR(50) NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    rag_collection VARCHAR(255),
    model_name VARCHAR(100) NOT NULL DEFAULT 'qwen2.5:7b',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    UNIQUE(org_id, code)
);

CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id);
CREATE INDEX IF NOT EXISTS idx_roles_code ON roles(org_id, code);
CREATE INDEX IF NOT EXISTS idx_roles_active ON roles(is_active) WHERE is_active = true;

-- ============================================
-- Users
-- ============================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255),
    role_id UUID REFERENCES roles(id) ON DELETE SET NULL,
    is_admin BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    avatar_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE,

    UNIQUE(org_id, username)
);

CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(org_id, username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active) WHERE is_active = true;

-- ============================================
-- Chats
-- ============================================
CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    type VARCHAR(20) NOT NULL DEFAULT 'main',  -- 'main', 'direct', 'group'
    name VARCHAR(255),                          -- Name for group chats
    participants TEXT[] NOT NULL DEFAULT '{}',  -- Array of user UUIDs as strings
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_chats_org ON chats(org_id);
CREATE INDEX IF NOT EXISTS idx_chats_type ON chats(type);
CREATE INDEX IF NOT EXISTS idx_chats_participants ON chats USING GIN(participants);
CREATE INDEX IF NOT EXISTS idx_chats_active ON chats(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_chats_last_message ON chats(last_message_at DESC NULLS LAST);

-- ============================================
-- Messages
-- ============================================
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    sender_type VARCHAR(20) NOT NULL DEFAULT 'user',  -- 'user', 'ai_role'
    sender_id UUID NOT NULL,                           -- User ID (or role owner for AI)
    content TEXT NOT NULL,
    mentions JSONB DEFAULT '[]',                       -- Array of mention objects
    reply_to_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    ai_validated BOOLEAN NOT NULL DEFAULT false,       -- AI response validated by user
    ai_edited BOOLEAN NOT NULL DEFAULT false,          -- AI response was edited
    is_deleted BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_unvalidated ON messages(sender_id, ai_validated)
    WHERE sender_type = 'ai_role' AND ai_validated = false;
CREATE INDEX IF NOT EXISTS idx_messages_active ON messages(is_deleted) WHERE is_deleted = false;

-- ============================================
-- Updated_at trigger function
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply triggers (drop first if exists to avoid errors on re-run)
DROP TRIGGER IF EXISTS update_organizations_updated_at ON organizations;
CREATE TRIGGER update_organizations_updated_at
    BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_roles_updated_at ON roles;
CREATE TRIGGER update_roles_updated_at
    BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_chats_updated_at ON chats;
CREATE TRIGGER update_chats_updated_at
    BEFORE UPDATE ON chats
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_messages_updated_at ON messages;
CREATE TRIGGER update_messages_updated_at
    BEFORE UPDATE ON messages
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Default data (for testing)
-- ============================================
-- Create a default organization
INSERT INTO organizations (id, name, slug, description)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'Demo Organization',
    'demo',
    'Default demo organization for testing'
) ON CONFLICT (slug) DO NOTHING;

-- Create default roles
INSERT INTO roles (id, org_id, name, code, system_prompt, model_name)
VALUES
(
    '00000000-0000-0000-0000-000000000101',
    '00000000-0000-0000-0000-000000000001',
    'Lawyer',
    'lawyer',
    'You are a corporate lawyer assistant. Help with legal questions, contract review, and compliance matters. Be precise and cite relevant laws when applicable. Answer in Russian.',
    'qwen2.5:7b'
),
(
    '00000000-0000-0000-0000-000000000102',
    '00000000-0000-0000-0000-000000000001',
    'Accountant',
    'accountant',
    'You are a corporate accountant assistant. Help with financial questions, tax matters, and accounting procedures. Be accurate and reference relevant accounting standards. Answer in Russian.',
    'qwen2.5:7b'
),
(
    '00000000-0000-0000-0000-000000000103',
    '00000000-0000-0000-0000-000000000001',
    'HR Manager',
    'hr',
    'You are an HR assistant. Help with employee relations, hiring processes, company policies, and workplace issues. Be supportive and reference company guidelines. Answer in Russian.',
    'qwen2.5:7b'
)
ON CONFLICT (org_id, code) DO NOTHING;

-- Create admin user (password: admin123)
-- bcrypt hash for 'admin123'
INSERT INTO users (id, org_id, name, username, email, password_hash, is_admin, role_id)
VALUES (
    '00000000-0000-0000-0000-000000000201',
    '00000000-0000-0000-0000-000000000001',
    'Admin User',
    'admin',
    'admin@demo.local',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4.NF/KBOqH0G5Wmu', -- admin123
    true,
    '00000000-0000-0000-0000-000000000101' -- Lawyer role
) ON CONFLICT (email) DO NOTHING;
