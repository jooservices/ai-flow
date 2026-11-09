-- Design note: purpose - record which delivered knowledge items a run reported using, so usefulness can be measured per item.
-- Access - by item (knowledge_deliveries_item_run, knowledge_uses_item_run), by project and time (knowledge_context_packs_project_created)
-- and finished verify runs per project (runs_project_verify_started). Growth - one row per run and delivered item, bounded by
-- runs x context-pack size; the cascade on knowledge_deliveries removes uses with their delivery, no extra retention rule.
-- Compatibility - additive only (new nullable column, new table, new indexes); the previous release ignores them.
-- Rollback - forward-only; the objects can stay unused.
-- Track reported use only against a delivered pack item; keep old token sizes explicitly unknown.
ALTER TABLE knowledge_context_packs
    ADD COLUMN IF NOT EXISTS token_count integer CHECK (token_count IS NULL OR token_count >= 0);

CREATE TABLE IF NOT EXISTS knowledge_uses (
    run_id      uuid NOT NULL,
    item_id     bigint NOT NULL,
    used_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, item_id),
    FOREIGN KEY (run_id, item_id) REFERENCES knowledge_deliveries(run_id, item_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS knowledge_deliveries_item_run ON knowledge_deliveries(item_id, run_id);
CREATE INDEX IF NOT EXISTS knowledge_uses_item_run ON knowledge_uses(item_id, run_id);
CREATE INDEX IF NOT EXISTS knowledge_context_packs_project_created
    ON knowledge_context_packs(project, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_project_verify_started
    ON runs(project, started_at DESC) WHERE step='verify' AND status='finished';
