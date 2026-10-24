# Tickets carry requirements only; the environment lives in the project configuration

**Console screens:** `projects`.

A ticket says what the product must do and how to know it is done. It never says which computer, server, host,
address, network, folder, account or tool to use, and never a Jira action ("write a comment"). Where and how
something runs belongs to the **environment section of the project configuration** and to the **environment of the
run**. A developer AI only develops the requirement; the flow gives it the environment. It never asks the owner about
servers.

## The ticket template (AC1)

The template headings (`prompts/v4/intake.md`, `aiflow/v4/worker/intake.py`) have no section for machines, hosts,
paths outside the repository, accounts or Jira actions. The *Environment* heading of a Bug asks for the affected
product, client and versions, never a machine.

## The check that marks a ticket ready (AC2)

`aiflow/v4/core/environment.py` finds wording that names a host (including public host names and URLs), an **IP
address** (IPv4 or IPv6), a **machine** (including generic server and host references), a **network**, an **access
instruction** or a **Jira action**. `intake.parse` rejects a rewrite that still contains any of it; a Jira note asks
the author to *describe the requirement instead* without repeating the server wording. The ticket stays in Intake.
The same requirement without the location or access detail is accepted. `intake.readiness_problems` returns the same
verdict for any description, and the open-ticket scan uses the same detector.

## Removing the wording and recording it (AC3, AC4)

The intake AI removes such wording, keeps the requirement behind it and answers with two optional lists before the
description:

```
REMOVED:
- machine: where the tests run -> the tests run against an isolated seeded site
ENVIRONMENT NEEDS:
- tests need a seeded site
```

* The Jira comment that keeps the original has every server wording replaced by `[server detail removed]`, then says
  what was removed (kinds and counts, the AI's own lines) and which environment ticket holds the need. A rejected
  rewrite gets a separate safe note telling the author how to fix the requirement; the ticket remains in Intake.
* `POST /v1/environment/removals` stores the full record (`environment_removals`) in the control database, never in
  Jira. The record also holds the wording found by the rule itself, not only the AI's list.
* A **real need** ("tests need a seeded site") is not left in the story: the control plane opens one Task labelled
  `environment` in the AI Flow project (`AIFLOW4_MAINTENANCE_PROJECT`, default `JAIF`) and links it to the story
  (*Relates*). The same need of the same project joins the existing ticket (`environment_needs`). If Jira creation
  fails, the need remains durable and the environment sweep retries it.

## The environment section of a project (AC5)

`projects/<KEY>.yaml` in the control repository:

```yaml
environment:
  - {name: WP_BASE_URL, kind: base_url, value: "<configured-base-url>", runs: [dev, verify]}
  - {name: WP_DB_NAME, kind: database, value: wp_test, runs: [dev]}
  - {name: BROWSERS, kind: browser, value: "chromium,firefox", runs: [verify]}
  - {name: WP_ADMIN_PASSWORD, kind: secret, runs: [dev, verify]}
```

| Field | Meaning |
| --- | --- |
| `name` | the environment variable, upper case (`WP_BASE_URL`) |
| `kind` | `service`, `base_url`, `database`, `browser` or `secret` |
| `value` | one line; required except for a secret. A value that carries a credential (`user:pass@`) is refused: list the credential as a secret |
| `runs` | which runs may use it: `dev`, `verify`, `post_merge` (default `dev` and `verify`) |

A **secret is listed by name only**. Its value lives on the machine in the root-only node-local project `.env` file
(`projects/<KEY>.env` under the AI Flow configuration folder, read by `worker/notes.py`) and is never in the
project file, the claim, the notes or the audit. Today the configuration lives in the control repository; later it
moves to the control database and the Machines and AI screen without changing these fields.

**Screen**: *Projects → Environment* edits the items (variable, kind, value, runs) and commits
`projects/<KEY>.yaml` to the control repository (permission `projects.manage`, audited by names only). Save and scan
notifications do not block the Projects screen controls while they are visible.

## What the run receives (AC6)

The work claim carries the items of the project (a secret without a value). The worker (`worker/notes.py: project_env`)
builds the run environment from only the configured values and secrets permitted for that run's step, plus the
explicit safe built-in `PLAYWRIGHT_BROWSERS_PATH` and explicit per-run values. Unlisted node-local variables are
never ambient run variables. The variables reach the AI process (`RunSpec.extra_env`). Run notes list the **names**,
their kind and whether each is set; never a value. A configured secret for `dev` is absent from a `verify` or
`post_merge` process. Jira notes and Workspace fields use environment item names and status; they do not expose a
node name or a temporary test-site URL.

## A missing environment item (AC8, AC9)

A developer AI that needs something that is not provided ends with

```
RESULT: BLOCKED: MISSING-ENV: WP_DB_NAME - the test database is not there
```

using a **name from the configuration**; free text is not an item and the run fails normally. The outcome is
`environment_missing`; the ticket goes back to To Do with a comment and **no question to the owner**. The control
plane records one open row per ticket and item (`environment_reports`) and does not offer the ticket again until the
item is provided. A question about servers, hosts, addresses, networks, access or where to run/test is classified
`missing_tool_or_environment` and answered by the flow (it points at configured item names and `MISSING-ENV`); it is
never posted to the owner. Intake also routes server questions to the environment flow instead of Jira's Needs owner
path. Only production or irreversible actions still reach a person.

The node agent returns the names of configured secrets it can read in its authenticated heartbeat; the control plane
confirms each item only for the assigned project, reporting node and current item fingerprint. This fenced,
names-only confirmation resolves matching waits and offers their tickets again; no secret value leaves the node.
Configuration changes, a removed item or a Done ticket also end a wait. If an item stays missing for an hour, the
environment sweep raises **one** environment ticket per project and item (label `environment`) and retries durable
environment needs whose Jira ticket could not yet be created. The **Projects** screen lists the missing items
(*Missing environment items*).

## Criteria that need the post-merge environment (AC10)

The reviewer marks a criterion that needs a post-merge environment and maps it to one exact command from the
project's configured checks. After a successful merge (including a human-review outcome), control queues the
criteria on that ticket, whether or not it belongs to a Jira version. A fenced `post_merge` claim gives the worker
only the project's environment items whose `runs` include `post_merge`, and the checks mapped to that ticket's
criteria. The worker runs each mapped command against the merged project checkout and reports each result through
`POST /v1/environment/post-merge/complete`. Control records a criterion as passed only when its own mapped command
passes; missing or failed commands are recorded failed. A deferred criterion without an exact configured check is
sent back for rework, and the control API refuses to accept a verify PASS carrying an unmapped criterion. *Projects →
Environment* shows pending, passed or failed criteria and when they ran. The `post_merge` permission is opt-in for each
item; it is not implied by `dev` or `verify`.

## Scan of the open tickets (AC11)

`environment.scan` checks every open ticket of every project and issue type, including Epics (summary and description), with the same rule and keeps
the report (`environment_scans`; kinds and counts, never the wording). Tickets nobody works on (Created, Backlog,
To Do, Needs owner) lose `aiflow-ready`, get a note that names the kinds and go back to **Intake**, where the intake AI
removes the wording and records it; tickets in flight are listed and left alone; tickets already in Intake are listed.
The watchdog runs it once a day; *Projects → Tickets that name servers* shows the last report and a *Scan now* button.

## Control API

- `GET /v1/projects/{key}/environment` reads project items, missing reports and post-merge check results.
- `PUT /v1/projects/{key}/environment` validates and saves items to the control repository.
- `GET /v1/environment/missing` returns missing items for the Projects screen.
- `GET /v1/environment/scan` reads the latest open-ticket scan; `POST /v1/environment/scan` runs and stores one.
- `POST /v1/work/claim` returns only the environment items permitted for that work step.
- `POST /v1/runs/{run_id}/result` records a missing item report or queues deferred criteria after a merge or successful human-review outcome.
- Authenticated node heartbeats confirm configured secret names by assigned project, node and item fingerprint; a confirmation resolves matching waits and reoffers tickets.
- `POST /v1/work/claim` offers pending post-merge criteria as fenced `post_merge` work with only permitted environment items and per-ticket mapped checks.
- `POST /v1/environment/post-merge/complete` records each criterion only from its mapped command; results are stored in `environment_post_merge_checks` against the original ticket.
- `POST /v1/github/token` returns the repository credential used to fetch the project checkout for dev, verify and post-merge work.
- `POST /v1/nodes/{name}/heartbeat` carries the authenticated node's names-only confirmation of configured environment secrets.
- `POST /v1/version-releases/claim` and `POST /v1/version-releases/{version_id}/complete` claim and complete configured version release work.
- `GET /v1/capacity` reports quota capacity for the Machines and AI dashboard; it is separate from project run
  environment configuration, which uses `GET /v1/projects/{key}/environment` and the work claim.

## Developer reference

| Piece | Where |
| --- | --- |
| rule, items, notes block | `aiflow/v4/core/environment.py` |
| project field | `aiflow/v4/core/controlrepo.py` (`Project.environment`) |
| intake check, record, environment needs | `aiflow/v4/worker/intake.py`, `prompts/v4/intake.md` |
| run environment and notes | `aiflow/v4/worker/notes.py`, `dev.py`, `verify.py` |
| reports, needs, scan | `aiflow/v4/control/environment.py`, migration `018_environment.sql` |
| tests | `tests/v4/test_environment.py` |
