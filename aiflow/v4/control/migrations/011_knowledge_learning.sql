-- Durable, scoped knowledge captured from completed work and curated by administrators.
CREATE TABLE IF NOT EXISTS knowledge_items (
    id                  bigserial PRIMARY KEY,
    project             text NOT NULL DEFAULT '',
    scope               text NOT NULL DEFAULT 'project' CHECK (scope IN ('project','general')),
    epic                text NOT NULL DEFAULT '',
    kind                text NOT NULL CHECK (kind IN ('study_case','decision','convention','skill','guide')),
    title               text NOT NULL,
    content             text NOT NULL,
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active','stale','retired')),
    approved            boolean NOT NULL DEFAULT false,
    approved_by         text NOT NULL DEFAULT '',
    created_by          text NOT NULL DEFAULT 'AI Flow',
    source_type         text NOT NULL DEFAULT 'manual',
    source_ref          text NOT NULL DEFAULT '',
    source_url          text NOT NULL DEFAULT '',
    ticket_key          text NOT NULL DEFAULT '',
    run_id              text NOT NULL DEFAULT '',
    pr_url              text NOT NULL DEFAULT '',
    mistake_signature   text NOT NULL DEFAULT '',
    repeated_mistake    boolean NOT NULL DEFAULT false,
    handed_count        integer NOT NULL DEFAULT 0 CHECK (handed_count >= 0),
    last_handed_at      timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    tsv                 tsvector GENERATED ALWAYS AS (to_tsvector('english', title || E'\n' || content)) STORED,
    embedding           vector(384),
    CHECK ((scope = 'general' AND project = '' AND epic = '') OR
           (scope = 'project' AND project <> ''))
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_source_unique
    ON knowledge_items (source_type, source_ref, kind) WHERE source_ref <> '';
CREATE INDEX IF NOT EXISTS knowledge_scope_status ON knowledge_items (scope, project, epic, status, kind);
CREATE INDEX IF NOT EXISTS knowledge_tsv ON knowledge_items USING gin (tsv);
CREATE INDEX IF NOT EXISTS knowledge_embedding_hnsw ON knowledge_items USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS knowledge_deliveries (
    run_id      uuid NOT NULL,
    item_id     bigint NOT NULL REFERENCES knowledge_items(id),
    project     text NOT NULL,
    ticket_key  text NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, item_id)
);
CREATE INDEX IF NOT EXISTS knowledge_deliveries_project ON knowledge_deliveries (project, delivered_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_context_packs (
    run_id      uuid PRIMARY KEY,
    project     text NOT NULL,
    ticket_key  text NOT NULL,
    epic        text NOT NULL DEFAULT '',
    item_count  integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
