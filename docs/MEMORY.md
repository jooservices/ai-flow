# Shared memory of the AIs

One memory for every configured project, machine and workspace, in PostgreSQL, behind a small service that is the only holder of the database credential.
Memory is **advice, never a requirement**: it stays off until a project has a service URL and the client token is configured. If the service or database is down, AI Flow carries on; reads return no advice and validated proposals wait in a local spool.

```
 AI (any machine)           supervisor / control                 aiflow-memory (HTTP)            PostgreSQL
 bin/aiflow-mem  ───────────► client token (search, propose) ───►  roles, namespaces, secret guard ──► memory_entry / _event / _feedback
 `MEMORY:` lines ──► supervisor proposes   admin token (case, verify, curate) ──►
```

## What is stored
| Kind | What | Written by |
|---|---|---|
| `fact` | workspace knowledge, a decision and why | an AI (proposed), or a person |
| `case` | a completed ticket: summary, what changed, why, study cases, rework and PR when available | the supervisor, automatically, when AI Flow moves the ticket to Done (no AI) |
| `lesson` | a cause, a trap, a rule learned | an AI (`MEMORY:` line or `aiflow-mem add`) |
| `context` | a summary of an epic or story | an AI or a person |

Scope: `global` (every company), `namespace` (one company), `project`, `ticket`. **A search sees the global entries and those of one namespace; another company's entries are not visible, and a token only works inside its namespaces.**

## Life of an entry
`fresh` (proposed by an AI, not shown in prompts) → `verified` (shown) → `promoted` (proven) → `decayed` (never deleted, never shown).
- A case written by the supervisor starts `verified`. It uses the latest developer hand-over when available and falls back to the ticket summary and completion context when it is not.
- The curator (plain rules, in the control, every 30 minutes): fresh with two different proposers or two helpful feedbacks → verified; verified with 3 helpful feedbacks → promoted; unhelpful (3+) → decayed; fresh and not confirmed in 30 days → decayed; expired → decayed.
- A ticket that is merged without any rework counts as "helpful" for the entries that were shown to its AIs.

## How the AIs use it
- The supervisor puts the best eligible verified entries for the ticket (project, repository, summary, AC, scopes) in the prompt under **Prior knowledge**. With no configured service, prompts say that shared memory is not configured and work proceeds normally.
- An AI can search more with `bin/aiflow-mem search "<words>"`, and proposes with `MEMORY: lesson | title | text` in its final message (at most 3) or `bin/aiflow-mem add ...`. A secret in an entry is refused.
- Roles: a **client** token searches and proposes (the AIs get this one in their environment); an **admin** token also writes verified entries, verifies, promotes, decays and curates (only the supervisor and the control hold it).

## Search by meaning (vectors)

Search finds an entry by its words and, when an embedding server is configured, also by its meaning ("automobile" finds a note about a car). The vectors are made by an Ollama server (`/api/embed`) and stored in PostgreSQL (pgvector, `memory_entry.embedding`, 1024 dimensions, cosine HNSW index).

- Settings of the service (`Workspaces/.env`, no secret): `AIFLOW_MEMORY_EMBED_URL` (empty = words only), `AIFLOW_MEMORY_EMBED_MODEL` (default `qwen3-embedding:0.6b`; any model answering 1024 dimensions, e.g. `mxbai-embed-large`), `AIFLOW_MEMORY_EMBED_MIN_SIMILARITY` (default 0.45: related text scored 0.5 to 0.75, unrelated text up to 0.37 with the default model).
- A background loop of the service gives a vector to every entry that has none, or has one of another model, including older decayed entries (changing the model fills everything again; old vectors are ignored meanwhile). It works in batches and retries after an Ollama outage. A write is never refused and never waits for Ollama.
- A search runs the words query and the meaning query inside the same scope and namespace filters, then merges both rankings (reciprocal rank fusion). Ollama down: the answer is the words result, as before.
- The same holds for the scoped notes (`search`).
- Embedding URLs, request text, vector values, and exception details are not written to service logs.

## Run it
- Database: PostgreSQL 14+ with `pg_trgm` and `unaccent` (and `vector` of pgvector for search by meaning; without it memory works by words only). The service creates its schema when it starts (`aiflow/memory/schema.sql`).
- Service: `bin/aiflow-memory` (uses `uv run --with pg8000`, nothing is installed on the system); unit `deploy/systemd/aiflow-memory.service`. Its settings are in `Workspaces/.env` (`AIFLOW_MEMORY_PG_*`) and its tokens in `JOOservices/config/memory-tokens.json` (git-ignored; example in `config.example/memory-tokens.json`).
- Project configuration: set `memory_url` in `[project]` of `config/aiflow/<KEY>.toml`; it may also be set in `[defaults]` or `[runtime]` as a fallback. The PostgreSQL user and password are only read by the service. The supervisor uses `AIFLOW_MEMORY_TOKEN` (admin); AI processes receive only `AIFLOW_MEMORY_AI_TOKEN` (client), plus the project URL and namespace defaults. `bin/aiflow-work check` reports whether shared memory is configured.
- Tests: `python3 -m unittest discover -s tests`; against a real database: `uv run --frozen python -m unittest tests.test_memory_pg` using the test database URL supplied to the run.
- Events in OpenObserve: `memory.recall`, `memory.propose`, `memory.case`, `memory.flush`, `memory.curate`.

Each configured memory service is curated by the control every 30 minutes. Project-specific service URLs use separate local proposal spools and independent curator leases. Network requests have a short timeout; a dead service is briefly suppressed so ticket work does not wait on repeated failed calls. Spool write errors are logged by type only and do not fail ticket work. Entries are checked for secrets before network calls or local spooling, and service error logs omit database exception text.

## The notes of a workspace (scoped notes)
The same service stores the workspace's scoped notes, projects and devices. Set `memory_url` for the project to select PostgreSQL; AI Flow passes `AIFLOW_MEMORY_SCOPED_STORAGE=postgres` to the project's AI process. The workspace CLI can call `bin/aiflow-mem scoped <operation> --arguments '<JSON object>'`; it uses the configured service URL and token and never connects to PostgreSQL directly. Scoped operations require an admin token. Use an exact namespace for a company's data; a limited admin can only read or change that namespace. Global notes are readable in every namespace, while writing or changing them requires an all-namespaces admin token.

Entries use integer ids, types `fact, decision, lesson, knowledge, idea, reference`, and lifecycle `fresh -> verified -> promoted -> decayed`. Search sees global + the selected namespace (+ the selected project); mutations require the exact global, namespace or project scope. Available operations are `add, get, search, list, update, status, stale, consolidate, stats, projects, project_update, device_add, device_list, device_update`, plus the one-time `import_entry, import_project, import_device` operations (idempotent through `origin`). The rules mirror the workspace's SQLite notes. PostgreSQL parity tests compare entries, projects and devices against an independent SQLite reference contract.

## Not yet
Importing the older notes (`scripts/mem` SQLite, Claude and Codex memory files) needs a separate plan approved by the owner.
