-- Migration 056: Remove pm role as subagent from all supervisors.
-- pm subagent functionality is being retired; supervisors handle tasks directly.

DELETE FROM role_subagents
WHERE subagent_role_id = (
    SELECT id FROM roles
    WHERE org_id = '00000000-0000-0000-0000-000000000000'::uuid AND code = 'pm'
);

UPDATE roles
SET agent_scope_description = '',
    updated_at = NOW()
WHERE org_id = '00000000-0000-0000-0000-000000000000'::uuid AND code = 'pm'
  AND agent_scope_description <> '';
