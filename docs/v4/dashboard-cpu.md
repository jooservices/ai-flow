# CPU use by machine on the Dashboard

**Console screens:** Command Center (`cc`)

Operators need to see what uses each node's CPU without signing in. The node agent samples processes on every
heartbeat and reports `metrics.cpu_breakdown` with four groups: AI tools, tests, browsers and databases. Each value
is the group's share of the whole machine, on the same 0–100 scale as `cpu_pct`.

## How a process is grouped

1. Match the process name to a known AI CLI (`claude`, `codex`, `opencode`, `gemini`), database server, browser, or
   test runner.
2. For interpreters (`python`, `node`, `php`, `uv`, …), read the command line: an AI CLI in the first two words is
   AI tools; a test runner marker anywhere is tests.
3. Everything else is ignored (system services stay out of the four groups).

To add a name, extend the frozensets or marker tuples in `aiflow/v4/node/cpu_breakdown.py` and cover the case in
`tests/v4/test_cpu_breakdown.py`.

## Dashboard panel

The Command Center AI and machines tab shows the **CPU use by machine** panel (`data-block="cpu-use"`). It loads
`GET /v1/cpu-overview` every 15 seconds. That route is open without a console session and returns only machine names,
online state, total CPU and the four group percentages.

## Routes

- `GET /v1/cpu-overview` Returns each registered machine's total CPU and the AI tools, tests, browsers and databases split without requiring a console session.
- `POST /v1/nodes/{name}/heartbeat` Stores the node's heartbeat metrics, including `cpu_breakdown`, and returns the desired accounts and settings for that machine.
