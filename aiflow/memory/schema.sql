-- AI memory schema. Idempotent: the service runs it at start. Nothing is ever deleted: an entry is decayed.
create extension if not exists pg_trgm;
create extension if not exists unaccent;

create or replace function mem_unaccent(text) returns text
    language sql immutable parallel safe as $$ select public.unaccent('public.unaccent', $1) $$;

create or replace function mem_tags(text[]) returns text
    language sql immutable parallel safe as $$ select array_to_string($1, ' ') $$;

create sequence if not exists memory_entry_seq;

create table if not exists memory_entry (
    id            uuid primary key default gen_random_uuid(),
    seq           bigint not null default nextval('memory_entry_seq'),
    kind          text not null check (kind in ('fact', 'case', 'lesson', 'context', 'decision', 'knowledge', 'idea', 'reference')),
    scope         text not null check (scope in ('global', 'namespace', 'project', 'ticket')),
    namespace     text not null default '',
    project       text not null default '',
    repo          text not null default '',
    jira_key      text not null default '',
    title         text not null check (char_length(title) between 1 and 200),
    body          text not null check (char_length(body) between 0 and 8000),
    tags          text[] not null default '{}',
    source        jsonb not null default '{}',
    status        text not null default 'fresh' check (status in ('fresh', 'verified', 'promoted', 'decayed')),
    confidence    real not null default 0.5,
    supersedes    uuid references memory_entry (id),
    expires_at    timestamptz,
    content_hash  text not null,
    created_by    text not null default '',
    confirmations int not null default 1,
    updated_by    text not null default '',
    origin        text,
    tags_raw      text,
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

-- Upgrade of a database created by an earlier version (idempotent): the Workspaces notes need an integer id, the extra kinds and shorter texts.
alter table memory_entry add column if not exists seq bigint not null default nextval('memory_entry_seq');
alter table memory_entry add column if not exists updated_by text not null default '';
alter table memory_entry add column if not exists origin text;
alter table memory_entry add column if not exists tags_raw text;
alter table memory_entry drop constraint if exists memory_entry_kind_check;
alter table memory_entry add constraint memory_entry_kind_check check (kind in ('fact', 'case', 'lesson', 'context', 'decision', 'knowledge', 'idea', 'reference'));
alter table memory_entry drop constraint if exists memory_entry_title_check;
alter table memory_entry add constraint memory_entry_title_check check (char_length(title) between 1 and 200);
alter table memory_entry drop constraint if exists memory_entry_body_check;
alter table memory_entry add constraint memory_entry_body_check check (char_length(body) between 0 and 8000);
create unique index if not exists memory_entry_seq_unique on memory_entry (seq);
create unique index if not exists memory_entry_origin_unique on memory_entry (origin) where origin is not null;

create table if not exists memory_project (
    id               bigserial primary key,
    namespace        text not null check (char_length(btrim(namespace)) > 0),
    name             text not null check (char_length(btrim(name)) > 0),
    description      text not null default '',
    tags             text not null default '',
    github_url       text not null default '',
    jira_key         text,
    jira_project_url text,
    published_as     text,
    version          text,
    stable           boolean not null default false,
    unique (namespace, name)
);

create table if not exists memory_device (
    id         bigserial primary key,
    uuid       uuid not null default gen_random_uuid(),
    namespace  text not null check (char_length(btrim(namespace)) > 0),
    name       text not null check (char_length(btrim(name)) > 0),
    role       text not null default '',
    host       text not null default '',
    ip         text not null default '',
    model      text not null default '',
    firmware   text not null default '',
    location   text not null default '',
    status     text not null default 'active' check (status in ('active', 'inactive', 'maintenance')),
    notes      text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (namespace, uuid),
    unique (namespace, name, location)
);
create index if not exists memory_device_namespace on memory_device (namespace, status);

-- Search by meaning (JAIF-83): vectors made by an embedding model (Ollama), 1024 dimensions (qwen3-embedding, mxbai-embed-large). Filled in the background,
-- an entry without a vector is found by words only.
create extension if not exists vector;
alter table memory_entry add column if not exists embedding vector(1024);
alter table memory_entry add column if not exists embedding_model text;
alter table memory_entry add column if not exists embedded_at timestamptz;
create index if not exists memory_entry_embedding on memory_entry using hnsw (embedding vector_cosine_ops);
