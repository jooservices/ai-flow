-- Self-healing findings/actions and operator intervention records (docs/v4/no-operator.md).
CREATE TABLE IF NOT EXISTS flow_doctor_findings (
    id              bigserial PRIMARY KEY,
    subject         text NOT NULL,
    cause           text NOT NULL,
    detail          text NOT NULL DEFAULT '',
    next_step       text NOT NULL DEFAULT '',
    first_seen      timestamptz NOT NULL DEFAULT now(),
    last_seen       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    UNIQUE (subject, cause)
);
CREATE INDEX IF NOT EXISTS flow_doctor_findings_open ON flow_doctor_findings (last_seen DESC)
    WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS flow_doctor_actions (
    id              bigserial PRIMARY KEY,
    action_key      text NOT NULL UNIQUE,
    finding_id      bigint REFERENCES flow_doctor_findings(id) ON DELETE SET NULL,
    ticket_key      text NOT NULL DEFAULT '',
    node            text NOT NULL DEFAULT '',
    account_id      text NOT NULL DEFAULT '',
    action          text NOT NULL,
    outcome         text NOT NULL DEFAULT '',
    detail          text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS flow_doctor_actions_created ON flow_doctor_actions (created_at DESC);

CREATE TABLE IF NOT EXISTS operator_interventions (
    id              bigserial PRIMARY KEY,
    source_key      text NOT NULL UNIQUE,
    happened_at     timestamptz NOT NULL DEFAULT now(),
    source          text NOT NULL,
    actor           text NOT NULL,
    action          text NOT NULL,
    target          text NOT NULL DEFAULT '',
    reason          text NOT NULL DEFAULT '',
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    bug_ticket_key  text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS operator_interventions_happened ON operator_interventions (happened_at DESC);
CREATE INDEX IF NOT EXISTS operator_interventions_pending_bug ON operator_interventions (created_at)
    WHERE bug_ticket_key = '';

-- Product connections are told apart by a server-side fact the client cannot choose: the database role it logged in
-- with. The control plane registers its own login role here (Database.migrate); every other role, including a person
-- who sets application_name to look like the product, is an operator. Give people their own database role.
CREATE TABLE IF NOT EXISTS audit_product_roles (
    role_name       text PRIMARY KEY,
    registered_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO audit_product_roles (role_name) VALUES (session_user) ON CONFLICT DO NOTHING;

-- Capture direct SQL changes made outside the control-plane connection pool. Store metadata only: statement text
-- can contain credentials and is intentionally not copied into the intervention record.
CREATE OR REPLACE FUNCTION aiflow4_capture_direct_sql_intervention() RETURNS trigger AS $$
DECLARE
    app_name text := coalesce(current_setting('application_name', true), '');
BEGIN
    IF EXISTS (SELECT 1 FROM audit_product_roles WHERE role_name = session_user) THEN
        RETURN NULL;
    END IF;
    INSERT INTO operator_interventions (source_key, source, actor, action, target, reason, details)
    VALUES ('db:' || txid_current()::text || ':' || TG_TABLE_NAME || ':' || TG_OP,
            'database', session_user, lower(TG_OP), TG_TABLE_NAME,
            format('Interactive database session issued %s against table %I; statement text and row values are excluded.',
                   lower(TG_OP), TG_TABLE_NAME),
            jsonb_build_object('table', TG_TABLE_NAME, 'operation', TG_OP, 'application_name', app_name))
    ON CONFLICT (source_key) DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- Attach the audit trigger to every table that lacks it. Database.migrate runs this again after every pass, so
-- tables created by later migrations are audited too.
CREATE OR REPLACE FUNCTION aiflow4_attach_audit_triggers() RETURNS integer AS $fn$
DECLARE t record; attached integer := 0;
BEGIN
    FOR t IN SELECT c.relname AS tablename FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind = 'r'
             AND c.relname NOT IN ('operator_interventions', 'flow_doctor_actions', 'flow_doctor_findings',
                                   'schema_migrations', 'audit_product_roles')
             AND NOT EXISTS (SELECT 1 FROM pg_trigger g WHERE g.tgrelid = c.oid AND g.tgname = 'aiflow4_operator_audit'
                             AND (g.tgtype & 32) <> 0)
    LOOP
        EXECUTE format('CREATE OR REPLACE TRIGGER aiflow4_operator_audit AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE '
                       'ON %I FOR EACH STATEMENT EXECUTE FUNCTION aiflow4_capture_direct_sql_intervention()',
                       t.tablename);
        attached := attached + 1;
    END LOOP;
    RETURN attached;
END;
$fn$ LANGUAGE plpgsql;
SELECT aiflow4_attach_audit_triggers();

-- Schema changes (CREATE/ALTER/DROP, including dropped triggers) by anyone but the product. Event triggers need a
-- superuser; where the migration role is not one, the table triggers above still audit data changes.
CREATE OR REPLACE FUNCTION aiflow4_capture_ddl_intervention() RETURNS event_trigger AS $$
DECLARE r record;
BEGIN
    IF EXISTS (SELECT 1 FROM audit_product_roles WHERE role_name = session_user) THEN
        RETURN;
    END IF;
    FOR r IN SELECT command_tag, object_type, object_identity FROM pg_event_trigger_ddl_commands()
             WHERE NOT in_extension AND coalesce(schema_name, '') NOT LIKE 'pg\_temp%'
             AND object_identity NOT LIKE 'pg\_temp%'
    LOOP
        INSERT INTO operator_interventions (source_key, source, actor, action, target, reason, details)
        VALUES ('ddl:' || txid_current()::text || ':' || r.command_tag || ':' || r.object_identity,
                'database', session_user, lower(r.command_tag), left(r.object_identity, 300),
                format('Interactive database session changed schema object %s with %s; SQL text is excluded.',
                       r.object_identity, lower(r.command_tag)),
                jsonb_build_object('command', r.command_tag, 'object_type', r.object_type))
        ON CONFLICT (source_key) DO NOTHING;
    END LOOP;
EXCEPTION WHEN undefined_table THEN
    NULL;  -- the audit table itself was dropped in this statement
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION aiflow4_capture_drop_intervention() RETURNS event_trigger AS $$
DECLARE r record;
BEGIN
    IF EXISTS (SELECT 1 FROM audit_product_roles WHERE role_name = session_user) THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_event_trigger_dropped_objects()
               WHERE original AND NOT is_temporary AND object_type = 'table'
               AND object_identity IN ('operator_interventions', 'public.operator_interventions')) THEN
        RAISE EXCEPTION 'operator_interventions is the append-only audit ledger and cannot be dropped by an operator'
            USING ERRCODE = '42501';
    END IF;
    FOR r IN SELECT tg_tag AS command_tag, object_type, object_identity FROM pg_event_trigger_dropped_objects()
             WHERE original AND NOT is_temporary AND object_identity IS NOT NULL
    LOOP
        INSERT INTO operator_interventions (source_key, source, actor, action, target, reason, details)
        VALUES ('drop:' || txid_current()::text || ':' || r.object_type || ':' || r.object_identity,
                'database', session_user, lower(r.command_tag), left(r.object_identity, 300),
                format('Interactive database session dropped schema object %s with %s; SQL text is excluded.',
                       r.object_identity, lower(r.command_tag)),
                jsonb_build_object('command', r.command_tag, 'object_type', r.object_type))
        ON CONFLICT (source_key) DO NOTHING;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'aiflow4_operator_ddl') THEN
        CREATE EVENT TRIGGER aiflow4_operator_ddl ON ddl_command_end EXECUTE FUNCTION aiflow4_capture_ddl_intervention();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'aiflow4_operator_drop') THEN
        CREATE EVENT TRIGGER aiflow4_operator_drop ON sql_drop EXECUTE FUNCTION aiflow4_capture_drop_intervention();
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE EXCEPTION 'Flow doctor audit requires a migration role that can create PostgreSQL event triggers; refusing to mark the migration applied without DDL auditing'
        USING ERRCODE = '42501';
END
$$;
