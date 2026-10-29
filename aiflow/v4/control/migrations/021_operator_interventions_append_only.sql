-- Human actions are evidence. Product bookkeeping may attach rows to a session/Bug, but operators cannot rewrite
-- or erase the ledger after the fact. Product identity is the database login recorded by migrations, not
-- application_name supplied by a client.
CREATE OR REPLACE FUNCTION aiflow4_guard_operator_interventions() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM audit_product_roles WHERE role_name = session_user) THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'operator_interventions is append-only for non-product database roles'
        USING ERRCODE = '42501';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS aiflow4_guard_operator_interventions_rows ON operator_interventions;
CREATE TRIGGER aiflow4_guard_operator_interventions_rows
    BEFORE UPDATE OR DELETE ON operator_interventions
    FOR EACH ROW EXECUTE FUNCTION aiflow4_guard_operator_interventions();

DROP TRIGGER IF EXISTS aiflow4_guard_operator_interventions_truncate ON operator_interventions;
CREATE TRIGGER aiflow4_guard_operator_interventions_truncate
    BEFORE TRUNCATE ON operator_interventions
    FOR EACH STATEMENT EXECUTE FUNCTION aiflow4_guard_operator_interventions();
