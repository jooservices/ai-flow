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

-- Capture direct SQL changes made outside the control-plane connection pool. Store metadata only: statement text
-- can contain credentials and is intentionally not copied into the intervention record.
CREATE OR REPLACE FUNCTION aiflow4_capture_direct_sql_intervention() RETURNS trigger AS $$
DECLARE
    app_name text := coalesce(current_setting('application_name', true), '');
BEGIN
    IF app_name LIKE 'aiflow4-%' THEN
        RETURN NULL;
    END IF;
    INSERT INTO operator_interventions (source_key, source, actor, action, target, reason, details)
    VALUES ('db:' || txid_current()::text || ':' || TG_TABLE_NAME || ':' || TG_OP,
            'database', session_user, lower(TG_OP), TG_TABLE_NAME,
            'Direct database change; no reason was supplied',
            jsonb_build_object('table', TG_TABLE_NAME, 'operation', TG_OP, 'application_name', app_name))
    ON CONFLICT (source_key) DO NOTHING;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
             AND tablename NOT IN ('operator_interventions', 'flow_doctor_actions', 'flow_doctor_findings',
                                   'schema_migrations')
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS aiflow4_operator_audit ON %I', t.tablename);
        EXECUTE format('CREATE TRIGGER aiflow4_operator_audit AFTER INSERT OR UPDATE OR DELETE ON %I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION aiflow4_capture_direct_sql_intervention()', t.tablename);
    END LOOP;
END;
$$;

ALTER TABLE role_health ADD COLUMN IF NOT EXISTS probation_unlimited boolean NOT NULL DEFAULT false;
