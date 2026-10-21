# AI Flow v4 deployment

The v4 control plane runs the API and loops; each node runs `aiflow4-node-agent`, which supervises one
`aiflow4-worker@<account>` unit per account on that node. Install a release with `bin/aiflow4-node-install`
(or `bin/aiflow4-deploy` from the control machine); the scripts live next to this file.

## Topics

One line per feature document in `docs/v4/` (add yours here; keep details in the topic file).

- [Needs owner triage](../../docs/v4/triage.md): master accounts answer developer questions or escalate them
- [Master monitoring](../../docs/v4/master.md): fenced Claude/Codex cycles monitor incidents, follow up, and escalate protected work
- [Versioned releases](../../docs/v4/release.md): tagged tarballs and GHCR images are checksummed, cosign-signed and verified before node installation
- [SA system health](../../docs/v4/sa-health.md): node and account health, capacity recommendations, credential expiry, blocked AI and network diagnostics
- [Team Lead requirement coverage and progress reports](../../docs/v4/team-lead-report.md): inspect epic AC coverage and export Markdown, CSV or printable HTML progress reports
- [Worktree cleanup](../../docs/v4/worktree-cleanup.md): nodes delete the worktrees of Done tickets and spent per-run checkouts
- [Develop channel](../../docs/v4/develop-channel.md): lab hosts opt in to unsigned builds of `develop` while there is no release
- [Business view](../../docs/v4/business.md): `/business` shows epic progress, tickets waiting on the owner and change requests with their AI impact assessment
- [Stakeholder review](../../docs/v4/stakeholder.md): `/stakeholder` opens configured environment links and records acceptance or UAT feedback in Jira

## Tools on a node

Every node needs the AI CLIs used by its accounts, on the `PATH` of the node agent and the workers. Install
them with the vendor's installer, then sign each account in with that account's own `HOME`.

| AI CLI | Binary | Installation |
|---|---|---|
| Claude Code | `claude` | `npm install -g @anthropic-ai/claude-code` (or the native installer from Anthropic) |
| Codex | `codex` | `npm install -g @openai/codex` (or the release binary from GitHub) |
| Grok | `grok` | `npm install -g @vibe-kit/grok-cli` |
| OpenCode | `opencode` | `curl -fsSL https://opencode.ai/install \| bash` (or `npm install -g opencode-ai`) |

Supporting tools reported by the node agent are `git`, `gh`, `node`, `uv` and `docker`; install the ones the
projects and release commands on the node need.

### Project stacks and missing tools

A project's `stack` in `aiflow-control/projects/<KEY>.yaml` selects the extra tools its nodes need:

| Stack | Extra tools |
|---|---|
| `php-8.3` | `php`, `composer`, `wp` |
| `python-3.12` | `python3`, `uv` |

Every node reports the node-wide tools above plus these stack tools, with each tool's version or `missing`,
in its heartbeat. The Command Center node drawer shows a missing required tool in red. While a tool is
missing, the watchdog opens a warning incident titled `node <name> misses tools for <PROJECT>` and closes
it when a later heartbeat reports the tool. If the node goes offline, the incident stays open because no
heartbeat has confirmed the tools; it also closes when the node or the project is removed. Add a stack to
the mapping in `aiflow/v4/core/toolchains.py` when a project starts using it.

### Claude policy relay

The node agent listens on `127.0.0.1:8471` for Claude's policy hook and forwards decisions to the control API.
It retries connection errors and 5xx responses for up to 60 seconds, and writes each decision to the node
agent journal (`journalctl -u aiflow4-node-agent`). The node heartbeat includes `metrics.policy_relay.enabled`
and `metrics.policy_relay.healthy`.

The relay is enabled by default. Set `AIFLOW4_POLICY_RELAY=0` in `/etc/aiflow/node.env` to have Claude call
the control URL directly; the node agent and worker units both read that file. Set it to `1` to enable the
relay explicitly.

### Codex code-mode host

Codex 0.163 and later cannot run shell commands without the `codex-code-mode-host` binary. The node agent
reports it with the other tool versions, so Command Center shows `missing` when it is absent.

Download `codex-code-mode-host` from the same GitHub release as the installed `codex` version
(<https://github.com/openai/codex/releases>) and put the executable next to the `codex` binary, in the same
directory on `PATH` (for example `/usr/local/bin`). Install both from the same release: a host from a
different release may not speak the protocol of the `codex` it runs beside.

## Ticket timeline

`GET /v1/tickets/{key}/timeline` shows where a ticket's time went. It is computed from the registry's events and
runs, not from Jira worklogs. The ticket drawer in the console shows the totals and a coloured bar; hover a
segment to see its kind, duration, times, status, account and outcome.

| Segment | Meaning |
|---|---|
| `queued` | waits for an AI: To Do with `aiflow-ready`, or In Verify, while no run works on it |
| `dev` | one dev run, from claim to result |
| `rework` | one dev run after a verify sent the ticket back (`round` 1, 2, ...) |
| `verify` | one verify run |
| `waiting` | waits on a person: Needs owner or Human review |
| `ai` | a run of another step (intake, master, qa) |

Totals: `lead_time_s` (first time ready to Done, or to now while open), `ai_s` (all runs), `queue_s`, `waiting_s`,
`rework_rounds` (verify runs that sent the ticket back), `runs` and `runs_by_outcome`. Time in other statuses
(Backlog, Intake, To Do without `aiflow-ready`) is in no segment.

Moves made through the control API are recorded as `jira.transition` events. Moves and `aiflow-ready` changes
made by people in Jira are recorded by the Jira sync as `ticket.status` events (`from_status`, `ready`), at most
one sync (20 s) late. Tickets from before the sync recorded `ticket.status` have no ready time, so their lead
time starts at the first known event or run.

## Intake refinement

Tickets written by people go to the **Intake** status first. Accounts with the `intake_refiner` role in
`aiflow-control/accounts.yaml` see them as step `intake` (after verify work, before development); `aiflow-hold`
and `explicit_only` work as for the other steps. The worker checks out the project's base branch and runs
`prompts/v4/intake.md` with the ticket text and the repository's `docs/SPEC.md` (when present). An intake run
may read files but never change them: the policy hook denies the editor tools and allows only inspection
commands in the shell (`ls`, `cat`, `grep`, `find` without `-exec`/`-delete`, `git log/show/diff/status`, ...;
no redirects or command substitution), ignoring the project's command allowlist. The run gets no project
environment (`/etc/aiflow/projects/<KEY>.env` is not passed to it). The AI answers with one of two results:

- **Rewrite**: the original description is kept as a Jira comment ("original description kept"), the
  description is replaced with the template (business requirement, acceptance criteria AC1.., technical notes,
  definition of done) through the fenced `POST /v1/jira/{key}/description`, the `aiflow-ready` label is added
  and the ticket moves to **To Do**. Run outcome `refined`.
- **Questions**: the questions are written as a comment and the ticket moves to **Needs owner**. Run outcome
  `questions`. Answer them in the description and move the ticket back to Intake for another pass.

`POST /v1/jira/{key}/description` takes `{"run_id", "fence", "body"}` like the other Jira writes: a stale fence
gets 409 (audited as refused), a run of another ticket or of a step other than `intake` gets 403 and an empty
body gets 400. A rewrite that lacks one of the four template headings (in order) or an `AC1` line under the
acceptance criteria is not saved. An intake run that ends without a usable answer (outcome `failed`, the summary
names what was missing) leaves the ticket in Intake;
it is offered again 10 minutes after the failure.

## Console (Command Center and Delivery)

`/cc` and `/delivery` use the full window width: a 16 px gutter on each side, 24 px on screens wider than
1400 px, and no sideways scrolling on phones (wide tables and the board scroll inside their own box).

The footer on every view shows the AI Flow version (the `ai_flow.ref` pinned in `versions.lock`, otherwise
the version of the running control API), the environment (`AIFLOW4_ENV`), the control repo branch, links
to OpenObserve (`/o2/`), Mailpit (`/mail/`) and Gatus, and the JOOservices copyright with the current year.
All values come from `/v1/state` (`version`, `env`, `control_repo`, `versions`, `links`). The Gatus link
defaults to `/gatus/`; set `AIFLOW4_GATUS_URL` on the control API when Gatus is published elsewhere.

### Daily analytics

`GET /v1/analytics/daily?days=14` (1 to 90 days, default 14; optional `project=KEY`) returns one row per
UTC day, project and account that had runs: `runs`, `done`, `failed`, `rework`, `merged`, `tokens_in`,
`tokens_out`, `cost_usd` and `median_run_s`. A day is the UTC date a run started, and the window includes
today. `failed` counts failed, blocked, timeout and lost runs, the same set as the Command Center 24 h total.
`runs` also counts runs that are still running and runs with any other outcome (quota, human, released).
The median uses finished runs only. The numbers are read live from the `runs` table, so there is no rollup.

The Delivery view shows the selected project in an **Analytics** panel: a 14-day inline SVG bar chart of runs
per day, split into done or merged, rework, failed and other, and a table with one row per day and account.
The panel refreshes every minute.

### OpenObserve dashboards

`o2/dashboards/aiflow-events.json` defines the **AI Flow events and runs** dashboard with panels for runs per
hour by outcome, AI time per account, policy denials, incidents opened and node heartbeats. The panels query
the `aiflow4_events` log stream populated by the control plane. Each successful node heartbeat is sent to
OpenObserve as a `node.heartbeat` event only; it is not stored in the registry `events` table, so the Command
Center event list and the table do not fill with heartbeats.

From the repository root on a host that can reach OpenObserve, set the same URL and basic-auth value used by
event shipping, then run the provisioner:

```sh
AIFLOW4_O2_URL=http://aiflow4-openobserve:5080/o2 O2_BASIC='<base64 username:password>' \
  uv run --frozen python deploy/v4/o2/provision.py
```

`AIFLOW4_O2_URL` is the OpenObserve base URL, including any reverse-proxy prefix such as `/o2`. `O2_BASIC` is
the base64 encoding of `username:password` (without the `Basic ` prefix). The provisioner uses the `default`
organization and folder, creates the dashboard when its title is absent, and updates the existing dashboard
using its OpenObserve ID and hash on later runs. The checked-in definition is authoritative, so provisioning
again replaces changes made directly to this dashboard in the UI.

## Memory (related past work)

Migration `002_memory` adds the `memory` table: one row per `(kind, ref)` with `kind` one of `ticket` (ref =
ticket key), `run_summary` (ref = run id; every finished run with a summary, whatever its outcome: `done`, `blocked`, `merged`,
`human`, `failed`, `rework`, ...; the text starts with step and outcome), `finding` (ref = run
id of a verify run that sent the ticket back for rework) and `answer` (reserved, not filled yet). The `tsv`
column is generated from `text` (`english` configuration) and has a GIN index. control-loops refreshes the
table every 60 s (leader key `memory_index`); a row is rewritten only when its text changed, so the job is
idempotent and cheap.

Search it with `GET /v1/memory/search?project=TESTWP&q=cache invalidation&limit=10` (`project` empty = every
project, `limit` 1 to 50, default 10). `q` takes web-search syntax (`"exact phrase"`, `-word`, `or`); hits are
ranked by `ts_rank` and carry `kind`, `ref`, `ticket`, `project`, `rank`, `created_at` and a `snippet` with the
matched words in `**bold**`. Like `/v1/state`, it is a read endpoint without a token.

The claim of a dev or verify run includes `related`: up to 3 hits of the same project that share words with the
ticket summary, the ticket's own entries excluded. The dev prompt lists them under "Related past work". Memory
is advice: when the lookup fails, the claim goes on with `related: []`.
