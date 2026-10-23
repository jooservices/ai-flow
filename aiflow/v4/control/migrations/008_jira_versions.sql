-- Jira release versions and the Fix versions selected on mirrored tickets.
CREATE TABLE IF NOT EXISTS versions (
    id              text PRIMARY KEY,
    project         text NOT NULL,
    name            text NOT NULL DEFAULT '',
    released        boolean NOT NULL DEFAULT false,
    release_date    date
);
CREATE INDEX IF NOT EXISTS versions_project ON versions (project, name);

CREATE TABLE IF NOT EXISTS ticket_versions (
    ticket_key      text NOT NULL REFERENCES tickets(key) ON DELETE CASCADE,
    version_id      text NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    PRIMARY KEY (ticket_key, version_id)
);
CREATE INDEX IF NOT EXISTS ticket_versions_version ON ticket_versions (version_id, ticket_key);
