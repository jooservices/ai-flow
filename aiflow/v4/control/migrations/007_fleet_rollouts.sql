-- Rolling node upgrades (docs/v4/fleet-upgrade.md): one rollout per fleet target release, its canary and waves.

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS release_protocol integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS fleet_rollouts (
    id              bigserial PRIMARY KEY,
    target          text NOT NULL,                       -- the release CURRENT names (vX.Y.Z)
    previous        text NOT NULL DEFAULT '',            -- the fleet release before this rollout
    status          text NOT NULL DEFAULT 'canary',      -- canary | rolling | completed | halted | superseded
    wave            integer NOT NULL DEFAULT 0,          -- 0 is the canary
    batch_size      integer NOT NULL DEFAULT 1,
    failed_node     text NOT NULL DEFAULT '',
    reason          text NOT NULL DEFAULT '',
    rollback        text NOT NULL DEFAULT '',            -- '' | pending | succeeded | failed
    started_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS fleet_rollouts_latest ON fleet_rollouts (id DESC);

CREATE TABLE IF NOT EXISTS fleet_rollout_nodes (
    rollout_id      bigint NOT NULL REFERENCES fleet_rollouts(id) ON DELETE CASCADE,
    node            text NOT NULL,
    wave            integer NOT NULL,
    status          text NOT NULL DEFAULT 'assigned',    -- assigned | healthy | failed | rolling_back | rolled_back | rollback_failed
    from_version    text NOT NULL DEFAULT '',            -- what the node ran before; its rollback target
    assigned_at     timestamptz NOT NULL DEFAULT now(),
    upgraded_at     timestamptz,                         -- first heartbeat that reported the target
    healthy_at      timestamptz,
    rollback_at     timestamptz,
    detail          text NOT NULL DEFAULT '',
    PRIMARY KEY (rollout_id, node)
);
