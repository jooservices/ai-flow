-- Keep an explicit opt-out for machines removed from a control-repo account. In particular, the configured
-- primary machine is otherwise reintroduced by the next overlay of accounts.yaml.
CREATE TABLE IF NOT EXISTS account_machine_exclusions (
    account_id  text NOT NULL,
    node        text NOT NULL,
    detached_by text NOT NULL DEFAULT '',
    detached_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, node)
);
