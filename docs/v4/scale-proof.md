# Scale proof

Customers will have many projects, so every release is measured at that size against written performance budgets.
A regression (any budget exceeded, or incorrect behaviour at this size) blocks the release and becomes tickets.

Code: `aiflow/v4/control/scale_proof.py` (budgets, verdicts, release attachment, tickets),
`aiflow/v4/control/scale_harness.py` (the scenario). Tests: `tests/v4/test_scale_proof.py`.

## Scenario

One run builds, against a real control API, PostgreSQL and Redis leases:

- **100 projects** (`SCP001`…`SCP100`), each with 20 earlier tickets (2000 in total, 70 % Done) and a finished run
  for every Done one.
- **50 machines**, each joined with a join token and heartbeating like a node agent; every machine serves 4
  projects and every project is served by 2 machines.
- **200 simulated AI sessions** (4 per machine: codex, claude, opencode, grok accounts). A session polls
  `/v1/work/candidates`, claims with `/v1/work/claim`, heartbeats, reports `done` and releases the lease. No AI
  provider is called and no real machine is needed.
- **Ticket traffic** in three waves (200, 80 and 80 tickets that become ready together): a morning burst that keeps
  about 170 sessions working at once, then trickles. About 5 % of the tickets are held (`aiflow-hold`) and about 5 %
  are blocked by an earlier ticket and become ready when it is Done. Priorities and projects are skewed like real
  traffic (some projects are far busier).

The traffic is a pure function of the configuration (`Scenario`, default seed 236): two runs with the same
configuration drive the same tickets in the same order. The report carries `plan_digest` (equal for equal
configurations) and the comparable `counts`; the timings differ only by the noise of the host.

## Budgets

The budgets live in one place, `BUDGETS` in `aiflow/v4/control/scale_proof.py`. The author did not say which
statistic they apply to; each budget states it here and in the code:

| Budget key | Budget | Limit | Statistic | How it is measured |
|---|---|---|---|---|
| `console_page_load` | Console pages load | 2 s | p95 per page, worst page | Each console page (`/cc`, `/delivery`, `/sa`, `/lead`, `/business`, `/review`, `/stakeholder`, `/knowledge`, `/projects`) is loaded 5 times at the busiest moment: the HTML plus the data call it renders from (`/v1/state`, `/v1/reviews`, `/v1/knowledge/resources`, `/v1/projects`), one after the other. The p95 of every page is computed; the slowest page counts. `/integrations` needs a live Jira and GitHub connection and is not part of the scenario. |
| `project_picker` | Project picker answers | 200 ms | p95 | `GET /v1/projects`, the list behind the project picker, 20 times at the busiest moment. |
| `ticket_pickup` | Ticket is picked up | 30 s | p95 | For every ticket of the traffic, the time from the ticket being ready to its claim being granted. All 200 sessions sweep for work in turn, so this is the time the control plane needs to serve the whole fleet. Blocked tickets count from the moment their blocker is Done. |

Why p95: a single slow outlier on a shared CI host should not block a release, while a real slowdown moves the 95th
percentile. The median and maximum are kept in the report next to the p95 for context. p95 is the nearest-rank
percentile. The slowest page decides the page budget, so one slow page cannot hide behind fast ones.

"Busiest moment" is right after the first wave was claimed: about 170 sessions are working and `/v1/state` lists
that many active runs.

A budget over its limit, a budget without a measurement, or a failed correctness check fails the run. The report
names the budget and the measured value, for example
`Console pages load: console_page_load p95 per page, worst page 3.200 s exceeds the budget of 2 s`.

## Correctness at this size

The run also fails when behaviour is wrong at 100/50/200: every ready ticket picked exactly once, held tickets never
picked, blocked tickets not started before their blocker finished, claims only on machines that serve the project,
fence 1 for every fresh ticket, a second claim of a leased ticket refused, no run left working, all 100 projects, 50
online machines and 200 sessions visible in `/v1/state`, all projects in the picker, and every working session
visible as an active run.

## Release attachment and gate

- CI runs `aiflow4 scale-proof --release vX.Y.Z --out dist/scale-proof-vX.Y.Z.json --submit CONTROL_URL` in the
  `release` job of `.github/workflows/release-v4.yml`, before anything is built or published. It creates a
  database and Redis key prefix of its own, prints every budget and exits **1** when the run failed, which stops the
  release. The CLI requires `--submit` and `AIFLOW4_OWNER_TOKEN`, and exits **3** if the result cannot be attached
  to the control plane. Exit 2: the scenario could not run.
- The JSON report is uploaded as an asset of the GitHub Release (`scale-proof-vX.Y.Z.json`).
- The release job requires repository secrets `AIFLOW4_CONTROL_URL` and `AIFLOW4_OWNER_TOKEN`; every release result
  is attached to Command Center: `POST /v1/scale-proof/results` (owner token) stores the run in `scale_proof_runs`,
  judges it again from its measurements (a submitted verdict is ignored) and files tickets. A missing secret or a
  failed submission stops the release job.
- A failed run blocks its release: a Jira version whose name equals the release is not ready for the automatic
  release flow (`version_release.evaluate`) and shows the reason `scale_proof_failed` in release progress, until a
  later run attached to the same release passes. A release without a run is not judged by the gate.

## Tickets from findings

Every failed budget and every failed correctness check is a finding with a stable signature (`budget:<key>`,
`correctness:<check>`). `file_tickets` creates one Bug in the flow-bug project (`TESTFLOW`, labels `flow-bug`,
`scale-proof`) per signature, with the budget, limit, measured value, release, run and scenario. No manual step:

- the same finding in a later run adds a comment and counts up (`scale_proof_findings.occurrences`), no second ticket;
- when its ticket is Done and the finding comes back, a new ticket is created;
- when Jira is unavailable the finding is kept with its error and every later submission retries it, even if that
  later run passes.

## Console

Command Center (`/cc`) has a **Scale proof** panel: each recorded run with its release, a *Within budget* or
*Regression* flag, every budget with statistic, limit, measured value and state, the failures, and links to the
created tickets. Release progress shows `scale_proof_failed` as a reason a version is not ready. Also readable at
`GET /v1/scale-proof` and in `GET /v1/state` (`scale_proof`). The run itself is a developer and CI command; end users
only read its result.

## Running it by hand

```
AIFLOW4_TEST_DATABASE_URL=postgresql://… AIFLOW4_TEST_REDIS_URL=redis://… \
  uv run --frozen aiflow4 scale-proof --release v4.0.0-local --out scale.json
```

A run takes about 1–2 minutes. `uv run --frozen pytest tests/v4/test_scale_proof.py -q` runs the scenario twice
(AC1 comparability), enforces the budgets and tests exceeded budgets, release blocking and ticket creation with
synthetic reports.
