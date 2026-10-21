CREATE TABLE IF NOT EXISTS managed_resources (
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    archived boolean NOT NULL DEFAULT false,
    path text NOT NULL,
    version integer NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    commit_sha text NOT NULL DEFAULT '',
    PRIMARY KEY (resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS managed_resource_history (
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    version integer NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    archived boolean NOT NULL DEFAULT false,
    actor text NOT NULL,
    action text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    commit_sha text NOT NULL DEFAULT '',
    pr_number integer,
    pr_url text NOT NULL DEFAULT '',
    approved_by text NOT NULL DEFAULT '',
    PRIMARY KEY (resource_type, resource_id, version)
);

CREATE TABLE IF NOT EXISTS managed_resource_proposals (
    id bigserial PRIMARY KEY,
    resource_type text NOT NULL,
    resource_id text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    archived boolean NOT NULL DEFAULT false,
    base_title text NOT NULL DEFAULT '',
    base_content text NOT NULL DEFAULT '',
    base_archived boolean NOT NULL DEFAULT false,
    base_version integer NOT NULL DEFAULT 0,
    actor text NOT NULL,
    action text NOT NULL,
    branch text NOT NULL,
    pr_number integer NOT NULL,
    pr_url text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'merged', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    merged_at timestamptz,
    approved_by text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS managed_resource_proposals_status ON managed_resource_proposals (status, created_at DESC);
