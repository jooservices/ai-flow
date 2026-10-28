# Scale proof

Customers will have many projects, so every release is measured at that size against written performance budgets.
A regression (any budget exceeded, or incorrect behaviour at this size) blocks the release and becomes tickets.

Code: `aiflow/v4/control/scale_proof.py` (budgets, verdicts, release attachment, tickets),
`aiflow/v4/control/scale_harness.py` (the scenario). Tests: `tests/v4/test_scale_proof.py`.

## Scenario

One run drives the real FastAPI control application in process and uses real PostgreSQL and Redis leases:

- **100 projects** (`SCP001`…`SCP100`), each with 20 earlier tickets (2000 in total, 70 % Done) and a finished run
  for every Done one.
- **50 machines**, each joined with a join token and heartbeating like a node agent; every machine serves 4
  projects and every project is served by 2 machines.
- **100 licensed AI accounts and 200 simulated sessions** (two concurrent sessions per Business-plan account).
  Sessions are spread across the 50 simulated machines and four provider types. Each sends a stable `worker_id` to
  `GET /v1/work/candidates`, claims with `POST /v1/work/claim`, heartbeats, reports `done` and releases the lease.
  This matches production's per-worker identity and tests concurrent sessions sharing one account. No AI provider or
  real machine is used.
- **Ticket traffic** in three waves (200, 80 and 80 tickets that become ready together): a morning burst that keeps
  about 170 sessions working at once, then trickles. About 5 % of the tickets are held (`aiflow-hold`) and about 5 %
  are blocked by an earlier ticket and become ready when it is Done. Priorities and projects are skewed like real
  traffic (some projects are far busier).

The traffic plan is a pure function of the configuration (`Scenario`, default seed 236): equal configurations have
the same tickets and `plan_digest`. Claims run concurrently, so which session wins a particular ticket can vary.
The report records comparable counts and the host timings.

This is an application scale proof, not a full deployment or network benchmark. FastAPI routes execute through
`TestClient` in the test process; data reads and writes use the dedicated PostgreSQL database and Redis namespace.
It creates no physical machines, does not start 200 operating-system processes, and calls no AI provider. At most
14 worker requests and 12 console API requests run at once (26 total): two console views, each with up to six
concurrent API calls. The database pool is capped at 40
connections. The report records these limits so each run's resource demand is explicit.
The Jira and GitHub App calls used by the readiness gate are local in-process stand-ins, not requests to those
providers. This run therefore measures the control API and its PostgreSQL/Redis work under simulated fleet traffic;
it does not measure provider availability, provider latency, physical machine capacity, or AI inference capacity.
The API still records run and knowledge rows, but local vector embeddings are disabled in this harness; its budgets
cover control-plane requests and PostgreSQL/Redis work, not embedding-model CPU time.

The harness measures all 18 top-level console views: `/cc`, `/work`, `/delivery`, `/sa`, `/lead`, `/business`,
`/review`, `/stakeholder`, `/machines`, `/users`, `/knowledge`, `/backup`, `/integrations`, `/projects`,
`/licensing`, `/notifications`, `/settings` and `/support`. For each view it measures the page route, common
console bootstrap calls and that view's data calls. Page and project-picker requests start behind a barrier with the
first bounded worker claim burst; the report fails if their measured intervals do not overlap or fewer than 150
worker sessions are active. Integrations read local configuration only; they do not contact Jira or GitHub.

## Budgets

The budgets live in one place, `BUDGETS` in `aiflow/v4/control/scale_proof.py`. Each budget states the statistic
here and in the code:

| Budget key | Budget | Limit | Statistic | How it is measured |
|---|---|---|---|---|
| `console_page_load` | Console views load | 2 s | p95 per view, worst view | All 18 console views are each loaded 3 times during the worker burst; each sample includes the view route, common bootstrap calls and view-specific data calls. Up to two views are measured concurrently, with at most six API calls per view, alongside 14 worker requests. The slowest view counts. |
| `project_picker` | Project picker answers | 200 ms | p95 | `GET /v1/projects`, the list behind the project picker, 5 times during the same worker burst. |
| `ticket_pickup` | Ticket is picked up | 30 s | p95 | For every ticket of the traffic, the time from the ticket being ready to its claim being granted. All 200 sessions sweep for work in turn, so this is the time the control plane needs to serve the whole fleet. Blocked tickets count from the moment their blocker is Done. |

The report uses the nearest-rank percentile and keeps the median and maximum for context. Each console view has 3
samples and the picker has 5, so p95 equals the maximum for those measurements; treat them as a short, conservative
signal rather than a stable percentile estimate. Ticket pickup has hundreds of samples, so its p95 describes the
tail of the traffic. The slowest view decides the view budget, so one slow view cannot hide behind fast ones.

"Busiest moment" is the first worker burst: at least 150 of the 200 simulated sessions must be active while the
console and picker samples run. The report records the actual active-run count and verifies that measured view
requests overlapped claim requests.

A budget over its limit, a budget without a measurement, or a failed correctness check fails the run. The report
names the budget and the measured value, for example
`Console views load: console_page_load p95 per view, worst view 3.200 s exceeds the budget of 2 s`.

## Correctness at this size

The run also fails when behaviour is wrong at 100/50/200: every ready ticket picked exactly once, held tickets never
picked, blocked tickets not started before their blocker finished, claims only on machines that serve the project,
fence 1 for every fresh ticket, a second claim of a leased ticket refused, no run left working, all 100 projects, 50
online machines, 100 licensed accounts and their 200 concurrent sessions visible in `/v1/state`, all projects in the
picker, and every working session visible as an active run.

## Release attachment and gate

- CI runs `aiflow4 scale-proof --release vX.Y.Z --out dist/scale-proof-vX.Y.Z.json --submit CONTROL_URL` in the
  `release` job of `.github/workflows/release-v4.yml`, before anything is built or published. The required
  `AIFLOW4_CONTROL_URL` and `AIFLOW4_OWNER_TOKEN` repository secrets attach its result. It creates a
  database and Redis key prefix of its own, prints every budget and exits **1** when the run failed, which stops the
  release. Exit 2: the scenario could not run; exit 3: the result could not be attached on the control plane.
- The JSON report is uploaded as an asset of the GitHub Release (`scale-proof-vX.Y.Z.json`).
- The repository secrets `AIFLOW4_CONTROL_URL` and `AIFLOW4_OWNER_TOKEN` are required. The result is attached to
  the release in Command Center: `POST /v1/scale-proof/results` (owner token) stores the run in `scale_proof_runs`,
  judges it again from its measurements (a submitted verdict is ignored) and files tickets.
- Every unreleased Jira version needs an attached passing proof before the automatic release flow can mark it
  ready. A failed run shows `scale_proof_failed`; no attached run shows `scale_proof_missing`. A later passing run
  clears either reason. Versions already marked released before this gate are grandfathered.

## Tickets from findings

Every failed budget and every failed correctness check is a finding with a stable signature (`budget:<key>`,
`correctness:<check>`). `file_tickets` creates one Bug in the flow-bug project (`TESTFLOW`, labels `flow-bug`,
`scale-proof`) per signature, with the budget, limit, measured value, release, run and scenario. Concurrent submissions
serialize filing by signature so only one Jira issue is created. No manual step:

- the same finding in a later run adds a comment and counts up (`scale_proof_findings.occurrences`), no second ticket;
- when its ticket is Done and the finding comes back, a new ticket is created;
- when Jira is unavailable the finding is kept with its error and retried on the next proof submission, even if that run passes; the ticket links back to the original finding run.

## Console

Command Center (`/cc`) has a **Scale proof** panel: each recorded run with its release, a *Within budget* or
*Regression* flag, every budget with statistic, limit, measured value and state, the failures, and links to the
created tickets. Release progress shows `scale_proof_missing` or `scale_proof_failed` as a reason a version is not
ready. Also readable at `GET /v1/scale-proof` and in `GET /v1/state` (`scale_proof`). The run itself is a developer
and CI command; end users only read its result.

Release workers list eligible versions with `GET /v1/version-releases/candidates`, claim through
`POST /v1/version-releases/claim`, and submit check results through `POST /v1/version-releases/{version_id}/complete`.
The control-machine deploy process reads `GET /v1/deploy/state`; the Business panel reads
`GET /v1/business/{key}/release-progress`.
The scale-proof record and read routes are `POST /v1/scale-proof/results` and `GET /v1/scale-proof`.

### API routes covered and referenced

These are the concrete control API routes used by the measured screens and worker scenario, plus related route
contracts changed alongside this feature. “Measured” means the harness sends that request during its timed run;
it does not mean every possible request or user action on that screen is load-tested.

| Route | Measured | Purpose |
|---|---:|---|
| `GET /v1/capacity` | Yes | Command Center capacity summary. |
| `GET /v1/dashboard/quota` | Yes | Command Center quota summary. |
| `GET /v1/dashboard/waiting` | Yes | Waiting-ticket data for the Command Center, Work and Delivery views. |
| `GET /v1/reviews` | Yes | Review queue data. |
| `GET /v1/reviews/export.csv` | No | Export review metrics. |
| `POST /v1/accounts/{account_id}/probe` | No | Record an account probe, including a newly reported quota forecast. Workers do not probe accounts on each poll. |
| `POST /v1/reviews/{key}/answer` | No | Record an owner’s answer in the review queue. |
| `POST /v1/reviews/{key}/approve` | No | Approve a review item. |
| `POST /v1/reviews/{key}/ask-ai` | No | Ask AI about a review item. |
| `POST /v1/reviews/{key}/reject` | No | Reject a review item. |
| `POST /v1/reviews/{key}/rework` | No | Send a review item back for rework. |
| `POST /v1/reviews/{key}/skip-gate` | No | Skip the review gate with an audited action. |
| `POST /v1/runs/{run_id}/result` | Yes | Submit each simulated worker’s run result and release its lease. |
| `GET /v1/runtime-settings` | Yes | Load the Runtime settings screen. |
| `POST /v1/runtime-settings` | No | Set a runtime value and queue its restart. |
| `GET /v1/deploy/state` | No | Read control-machine deployment state. |
| `POST /v1/version-releases/claim` | No | Claim a version-release check. |
| `GET /v1/backups` | No | Read backup status and configured targets. |
| `PUT /v1/backups/config` | No | Save backup configuration. |
| `GET /v1/github/checks` | No | Read checks for a pull request. |
| `GET /v1/github/ci-log` | No | Read a bounded CI job log. |
| `GET /v1/github/git/{git_path:path}` | No | Read a GitHub repository path. |
| `GET /v1/github/pr` | No | Read pull request details. |
| `POST /v1/github/comment` | No | Add a pull request comment. |
| `POST /v1/github/delete-branch` | No | Delete a remote branch. |
| `POST /v1/github/git/{git_path:path}` | No | Write a GitHub repository path. |
| `POST /v1/github/merge` | No | Merge a pull request. |
| `POST /v1/github/pr` | No | Create a pull request. |
| `POST /v1/github/token` | No | Store a GitHub access token. |
| `POST /v1/github/update-branch` | No | Update a pull request branch. |
| `GET /v1/master/autonomy` | No | Read the master agent's autonomy settings. |
| `GET /v1/master/candidate` | No | Read the current master-agent candidate. |
| `GET /v1/master/context` | No | Read the master agent's decision context. |
| `PUT /v1/master/autonomy/budget` | No | Change the master agent's autonomy budget. |
| `POST /v1/knowledge/proposals/{proposal_id}/approve` | No | Approve a knowledge proposal. |
| `POST /v1/knowledge/resources` | No | Create or update a knowledge resource. |
| `POST /v1/knowledge/resources/{kind}/{resource_id}/restore` | No | Restore a prior knowledge-resource version. |

Routes marked “No” are listed to document the changed API contracts; the scale proof does not invoke them or count
them as benchmark evidence. Measured requests run against the in-process FastAPI app with the dedicated PostgreSQL
and Redis services described above.

**Console screens:** `cc`, `business`

## Running it by hand

```
export AIFLOW4_TEST_DATABASE_URL='postgresql://…/aiflow4_wt236'
export AIFLOW4_TEST_REDIS_URL='redis://…/6'
export AIFLOW4_TEST_REDIS_PREFIX='aiflow4_wt236:'
export AIFLOW4_CONTROL_URL='https://control.example'
export AIFLOW4_OWNER_TOKEN='…'
uv run --frozen aiflow4 scale-proof --release v4.0.0-local --out scale.json \
  --submit "$AIFLOW4_CONTROL_URL"
```

Run duration depends on the host and database; the JSON report records `duration_s`. The test module runs the full
100/50/200 scenario once, checks every console view with a smaller local fleet, enforces the budgets, and tests
release blocking and ticket creation with synthetic reports.
