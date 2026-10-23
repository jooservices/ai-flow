# Tickets carry requirements only; the environment lives in the project configuration

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

`aiflow/v4/core/environment.py` finds wording that names a host (internal host names, `ssh://`), an **IP address**, a
**machine** ("the Dev VM", "shared Docker", "lab machines", "WSL"), a **network** (VPN, subnet, bastion, port
forwarding), an **access instruction** ("ssh into", "log in to the server", `password: …`) or a **Jira action**.
`intake.parse` rejects a rewrite that contains any of it; the author is told to *describe the requirement instead*,
and the rejection names the kinds and the quoted words. The ticket stays in Intake. The same requirement without the
host is accepted. `intake.readiness_problems` returns the same verdict for any description.

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
  what was removed (kinds and counts, the AI's own lines) and which environment ticket holds the need.
* `POST /v1/environment/removals` stores the full record (`environment_removals`) in the control database, never in
  Jira. The record also holds the wording found by the rule itself, not only the AI's list.
* A **real need** ("tests need a seeded site") is not left in the story: the control plane opens one Task labelled
  `environment` in the AI Flow project (`AIFLOW4_MAINTENANCE_PROJECT`, default `JAIF`) and links it to the story
  (*Relates*). The same need of the same project joins the existing ticket (`environment_needs`).

## The environment section of a project (AC5)

`projects/<KEY>.yaml` in the control repository:

```yaml
environment:
  - {name: WP_BASE_URL, kind: base_url, value: "http://site.example.test:8080", runs: [dev, verify]}
  - {name: WP_DB_NAME, kind: database, value: wp_test, runs: [dev]}
  - {name: BROWSERS, kind: browser, value: "chromium,firefox", runs: [verify]}
  - {name: WP_ADMIN_PASSWORD, kind: secret, runs: [dev, verify]}
```

| Field | Meaning |
| --- | --- |
| `name` | the environment variable, upper case (`WP_BASE_URL`) |
| `kind` | `service`, `base_url`, `database`, `browser` or `secret` |
| `value` | one line; required except for a secret. A value that carries a credential (`user:pass@`) is refused: list the credential as a secret |
| `runs` | which runs may use it: `dev`, `verify` (default both) |

A **secret is listed by name only**. Its value lives on the machine in the root-only node-local project `.env` file
(`projects/<KEY>.env` under the AI Flow configuration folder, read by `worker/notes.py`) and is never in the
project file, the claim, the notes or the audit. Today the configuration lives in the control repository; later it
moves to the control database and the Machines and AI screen without changing these fields.

**Screen**: *Projects → Environment* edits the items (variable, kind, value, runs) and commits
`projects/<KEY>.yaml` to the control repository (`GET`/`PUT /v1/projects/<KEY>/environment`, permission
`projects.manage`, audited by names only).

## What the run receives (AC6)

The claim carries the items of the project (a secret without a value). The worker (`worker/notes.py: project_env`)
builds the run environment: `PLAYWRIGHT_BROWSERS_PATH`, the node-local `.env` file, then the configured values of the
items the run's step may use, and the secrets those items name from the `.env` file. The variables reach the AI
process (`RunSpec.extra_env`). The run notes (the prompt of the dev and verify runs) list the **names**, their kind
and whether each is set; never a value. Nothing about a server is in a ticket, a source repository or the notes.

## A missing environment item (AC8, AC9)

A developer AI that needs something that is not provided ends with

```
RESULT: BLOCKED: MISSING-ENV: WP_DB_NAME - the test database is not there
```

using a **name from the configuration**; free text is not an item and the run fails normally. The outcome is
`environment_missing`; the ticket goes back to To Do with a comment and **no question to the owner**. The control
plane records one open row per ticket and item (`environment_reports`) and does not offer the ticket again until the
item is provided. A question about servers, hosts, networks or access is classified `missing_tool_or_environment`
and answered by the flow (it points at the variables and `MISSING-ENV`); only production or irreversible actions
still reach a person.

The flow doctor (JAIF-260) provides the item by calling `environment.provide(ctx, project, item, how)`, which offers
the waiting tickets again. If the item is still missing after one hour, the watchdog raises **one** environment
ticket per project and item (label `environment`). A change of the item in the configuration, a removed item or a
Done ticket also ends the wait. The **Projects** screen lists the missing items (*Missing environment items*).

## Criteria that need a particular machine (AC10)

The dev and verify prompts say that an acceptance criterion that can only be proven on one particular machine is
verified by the flow after the merge (canary, release checks), not by the developer run: implement it, test what runs
in the run, and say so in the summary. It is not a reason to stop or to ask.

## Scan of the open tickets (AC11)

`environment.scan` checks every open ticket of every project (summary and description) with the same rule and keeps
the report (`environment_scans`; kinds and counts, never the wording). Tickets nobody works on (Created, Backlog,
To Do, Needs owner) lose `aiflow-ready`, get a note that names the kinds and go back to **Intake**, where the intake AI
removes the wording and records it; tickets in flight are listed and left alone; tickets already in Intake are listed.
The watchdog runs it once a day; *Projects → Tickets that name servers* shows the last report and a *Scan now* button
(`GET`/`POST /v1/environment/scan`).

## Developer reference

| Piece | Where |
| --- | --- |
| rule, items, notes block | `aiflow/v4/core/environment.py` |
| project field | `aiflow/v4/core/controlrepo.py` (`Project.environment`) |
| intake check, record, environment needs | `aiflow/v4/worker/intake.py`, `prompts/v4/intake.md` |
| run environment and notes | `aiflow/v4/worker/notes.py`, `dev.py`, `verify.py` |
| reports, needs, scan | `aiflow/v4/control/environment.py`, migration `018_environment.sql` |
| tests | `tests/v4/test_environment.py` |
