-- Supervisor subagent role mapping
-- Defines which roles a supervisor role may call as subagents.

ALTER TABLE roles
  ADD COLUMN as_subagent_description TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS role_subagents (
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    subagent_role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,

    PRIMARY KEY (role_id, subagent_role_id),
    CHECK (role_id <> subagent_role_id)
);
