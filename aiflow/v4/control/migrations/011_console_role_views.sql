-- Multi-role console users, project restrictions, and the JAIF-222 role vocabulary.
ALTER TABLE console_users ADD COLUMN IF NOT EXISTS roles text[];
ALTER TABLE console_users ADD COLUMN IF NOT EXISTS project_keys text[];

UPDATE console_users
SET role = CASE role
    WHEN 'super_admin' THEN 'administrator'
    WHEN 'owner' THEN 'administrator'
    WHEN 'lead' THEN 'team_lead'
    ELSE role
END;

UPDATE console_users
SET roles = ARRAY[role]
WHERE roles IS NULL OR cardinality(roles) = 0;

ALTER TABLE console_users ALTER COLUMN roles SET DEFAULT ARRAY['administrator']::text[];
ALTER TABLE console_users ALTER COLUMN roles SET NOT NULL;
ALTER TABLE console_users ADD CONSTRAINT console_users_roles_not_empty CHECK (cardinality(roles) > 0);
CREATE INDEX IF NOT EXISTS console_users_roles ON console_users USING gin (roles);
