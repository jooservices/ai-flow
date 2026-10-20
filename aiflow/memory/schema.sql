-- AI memory schema. Idempotent: the service runs it at start. Nothing is ever deleted: an entry is decayed.
create extension if not exists pg_trgm;
create extension if not exists unaccent;

create or replace function mem_unaccent(text) returns text
    language sql immutable parallel safe as $$ select public.unaccent('public.unaccent', $1) $$;

create or replace function mem_tags(text[]) returns text
    language sql immutable parallel safe as $$ select array_to_string($1, ' ') $$;

create table if not exists memory_entry (
    id            uuid primary key default gen_random_uuid(),
    kind          text not null check (kind in ('fact', 'case', 'lesson', 'context')),
    scope         text not null check (scope in ('global', 'namespace', 'project', 'ticket')),
    namespace     text not null default '',
    project       text not null default '',
    repo          text not null default '',
    jira_key      text not null default '',
    title         text not null check (char_length(title) between 3 and 200),
    body          text not null check (char_length(body) between 3 and 8000),
    tags          text[] not null default '{}',
    source        jsonb not null default '{}',
    status        text not null default 'fresh' check (status in ('fresh', 'verified', 'promoted', 'decayed')),
    confidence    real not null default 0.5,
    supersedes    uuid references memory_entry (id),
    expires_at    timestamptz,
    content_hash  text not null,
    created_by    text not null default '',
    confirmations int not null default 1,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    verified_at   timestamptz,
    decayed_at    timestamptz,
    decay_reason  text not null default '',
    last_used_at  timestamptz,
    use_count     int not null default 0,
    helpful       int not null default 0,
    unhelpful     int not null default 0,
    tsv           tsvector generated always as (
        setweight(to_tsvector('simple', mem_unaccent(title)), 'A') ||
        setweight(to_tsvector('simple', mem_unaccent(body)), 'B') ||
        setweight(to_tsvector('simple', mem_unaccent(mem_tags(tags))), 'C')) stored,
    check (scope <> 'global' or (namespace = '' and project = '')),
    check (scope <> 'namespace' or (namespace <> '' and project = '')),
    check (scope not in ('project', 'ticket') or (namespace <> '' and project <> ''))
);

create unique index if not exists memory_entry_unique_live on memory_entry (namespace, project, content_hash) where status <> 'decayed';
create index if not exists memory_entry_tsv on memory_entry using gin (tsv);
create index if not exists memory_entry_title_trgm on memory_entry using gin (title gin_trgm_ops);
create index if not exists memory_entry_tags on memory_entry using gin (tags);
create index if not exists memory_entry_scope on memory_entry (namespace, project, status, kind);
create index if not exists memory_entry_jira on memory_entry (jira_key) where jira_key <> '';

create table if not exists memory_event (
    id       bigserial primary key,
    at       timestamptz not null default now(),
    actor    text not null default '',
    action   text not null,
    entry_id uuid,
    detail   jsonb not null default '{}'
);
create index if not exists memory_event_entry on memory_event (entry_id, at);

create table if not exists memory_feedback (
    id       bigserial primary key,
    at       timestamptz not null default now(),
    entry_id uuid not null references memory_entry (id),
    jira_key text not null default '',
    helpful  boolean not null,
    actor    text not null default ''
);
create index if not exists memory_feedback_entry on memory_feedback (entry_id);
