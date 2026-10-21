# AI Flow v4 deployment

The v4 control plane runs the API and loops; each node runs `aiflow4-node-agent`, which supervises one
`aiflow4-worker@<account>` unit per account on that node. Install a release with `bin/aiflow4-node-install`
(or `bin/aiflow4-deploy` from the control machine); the scripts live next to this file.

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

### Codex code-mode host

Codex 0.163 and later cannot run shell commands without the `codex-code-mode-host` binary. The node agent
reports it with the other tool versions, so Command Center shows `missing` when it is absent.

Download `codex-code-mode-host` from the same GitHub release as the installed `codex` version
(<https://github.com/openai/codex/releases>) and put the executable next to the `codex` binary, in the same
directory on `PATH` (for example `/usr/local/bin`). Install both from the same release: a host from a
different release may not speak the protocol of the `codex` it runs beside.

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
the `aiflow4_events` log stream populated by the control plane. Node heartbeat data is recorded once per
successful heartbeat.

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
