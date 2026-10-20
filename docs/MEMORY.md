# Shared memory of the AIs

One memory for every machine and workspace, in PostgreSQL, behind a small service that is the only holder of the database credential.
Memory is **advice, never a requirement**: when the service or the database is down the AIs work without it and nothing is lost (entries wait in a local spool).

```
 AI (any machine)           supervisor / control                 aiflow-memory (HTTP)            PostgreSQL
 bin/aiflow-mem  ───────────► client token (search, propose) ───►  roles, namespaces, secret guard ──► memory_entry / _event / _feedback
 `MEMORY:` lines ──► supervisor proposes   admin token (case, verify, curate) ──►
```

## What is stored
| Kind | What | Written by |
|---|---|---|
| `fact` | workspace knowledge, a decision and why | an AI (proposed), or a person |
| `case` | a ticket that is merged and Done: problem, what changed, why, study cases, rework, PR | the supervisor, automatically, when the ticket is Done (no AI, no tokens) |
| `lesson` | a cause, a trap, a rule learned | an AI (`MEMORY:` line or `aiflow-mem add`) |
| `context` | a summary of an epic or story | an AI or a person |

Scope: `global` (every company), `namespace` (one company), `project`, `ticket`. **A search sees the global entries and those of one namespace; another company's entries are not visible, and a token only works inside its namespaces.**

## Life of an entry
`fresh` (proposed by an AI, not shown in prompts) → `verified` (shown) → `promoted` (proven) → `decayed` (never deleted, never shown).
- A case written by the supervisor starts `verified` (a merged pull request is the evidence).
- The curator (plain rules, in the control, every 30 minutes): fresh with two different proposers or two helpful feedbacks → verified; verified with 3 helpful feedbacks → promoted; unhelpful (3+) → decayed; fresh and not confirmed in 30 days → decayed; expired → decayed.
- A ticket that is merged without any rework counts as "helpful" for the entries that were shown to its AIs.

## How the AIs use it
- The supervisor puts the best verified entries for the ticket (project, repository, summary, AC, scopes) in the prompt under **Prior knowledge**.
- An AI can search more with `bin/aiflow-mem search "<words>"`, and proposes with `MEMORY: lesson | title | text` in its final message (at most 3) or `bin/aiflow-mem add ...`. A secret in an entry is refused.
- Roles: a **client** token searches and proposes (the AIs get this one in their environment); an **admin** token also writes verified entries, verifies, promotes, decays and curates (only the supervisor and the control hold it).

## Run it
- Database: any PostgreSQL 14+ (the shared Docker of a machine for development: `scripts/docker/shared provision-project ai-memory --postgres`). Extensions `pg_trgm` and `unaccent`; the service creates its tables when it starts (`schema.sql`).
- Service: `bin/aiflow-memory` (uses `uv run --with pg8000`, nothing is installed on the system); unit `deploy/systemd/aiflow-memory.service`. Its settings are in `Workspaces/.env` (`AIFLOW_MEMORY_PG_*`) and its tokens in `JOOservices/config/memory-tokens.json` (git-ignored; example in `config.example/memory-tokens.json`).
- Clients: `memory_url` in aiflow.toml `[runtime]` and `AIFLOW_MEMORY_TOKEN` / `AIFLOW_MEMORY_AI_TOKEN` in `Workspaces/.env`. `bin/aiflow-work check` shows whether memory is on.
- Tests: `python3 -m unittest discover -s tests`; against a real database: `uv run --with pg8000 python -m unittest tests.test_memory_pg` (database `ai_memory_test`).
- Events in OpenObserve: `memory.recall`, `memory.propose`, `memory.case`, `memory.flush`, `memory.curate`.

## Not yet
Semantic (vector) search needs the `pgvector` extension, which the shared PostgreSQL image does not have; search is full-text plus trigram. Importing the older notes (`scripts/mem` SQLite, Claude and Codex memory files) needs a separate plan approved by the owner.
