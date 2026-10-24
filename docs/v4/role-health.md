# Role health: quality by AI account and role

AI Flow stops using an AI account for a role where it keeps failing, keeps the account's other roles available, and
brings the role back when the account recovers. Health is scoped to one **AI account and one role** (`dev`, `verify`,
`master`, `intake_refiner`). Code: `aiflow/v4/control/role_health.py`; the workers need no change because they only
receive the work the control API offers.

## How a run is scored

The role of a run follows its step: `dev` → dev, `verify` → verify, `triage` and `master` → master, `intake` and
`impact` → intake_refiner.

| Result of the run | Scored as |
|---|---|
| `merged`; a dev run that opened a pull request; a dev run whose PR verification accepted (merged, or passed and waiting for a person) | success |
| any other finished run that delivered its result (verify verdict, intake, triage answer, master cycle, impact) | success |
| a legacy dev run without a recorded PR URL whose verification sent it back (`rework`) | half a failure (`sent back`) |
| clean `no_changes`, or a legacy failed run whose summary starts with `no changes` | success |
| `failed`; `blocked` (the AI stopped without changing code) | failure |
| `timeout` or `lost` (stopped by the time limit or a lost lease) | a quarter of a failure (`stopped`) |
| a finished dev run not yet judged by verification | not counted yet |
| `quota`, or an error kind of quota, sign-in, infrastructure, network or tool problems | **excluded** |

The dev worker classifies a result as `tool_missing` only when the AI uses the explicit `MISSING-TOOL(S)` or
`MISSING-SERVICE(S)` marker, or an adapter records a failed command execution whose invoked catalogue tool matches
the shell's `command not found` output. A sentence that quotes an example error, or output printed by `echo` or
`printf`, is not execution evidence. General summary text is never scanned for tool names or the word "missing".
Role health rechecks stored evidence before counting `tool_missing`: legacy rows without an explicit marker or
normalized failed-command evidence are excluded from the score, so old false positives stop affecting recovery.
Pull requests and clean `no_changes` results are successes even if an older row has a failure outcome.

## Switching a role off

When at least `window` scored runs exist (default 10), the failure score is
`(failed + 0.5 × sent back + 0.25 × stopped) / runs` over the latest `window` scored runs of that account and role. Above the
threshold (default 60 %, strictly above), the role may be switched off for that account only, subject to pool capacity:

- the role keeps at least two ready accounts enabled, and no more than half of the ready role pool can be switched
  off. Ready accounts are unpaused, in a usable account state, and assigned to at least one online machine;
- when every eligible account has a complete score above the threshold, no account is switched off. One role-level
  `role_health` incident names the most frequent failure kind and lists counts for `failed`, `timeout`,
  `tool_missing` and `rework`, plus the tickets involved;
- the control API stops offering that role's work to the account; other roles keep working, and the watchdog's
  role-coverage check no longer counts the account for that role;
- the account row shows a note, and a `role_health` incident (warning) is opened and sent through the normal alert
  channels; it closes, with an "info" alert, when the role is switched back on.

The check runs after every run result, when an owner saves thresholds, and every 15 s in the watchdog. It evaluates
the whole role pool after each result so simultaneous failures cannot evade the capacity rule.

## Probation and recovery

A switched-off role gets one unresolved probation ticket at a time, with no daily cooldown. An eligible ticket is a Bug or Chore that is small
(description up to 2000 characters), whose description asks for a unit test, with no UI (no `ui`, `ux`, `frontend`
or `tier-senior` label and no UI wording such as page, button, screen, browser). It is taken from the tickets the
role would normally work (for example `To Do` + `aiflow-ready` for dev). A probation run that ends on quota or a
machine problem gives the slot back.

Probation succeeds when the run is a full success (a dev run needs verification to accept it; sent back is not a
success). After **two probation tickets in a row succeed** the role is switched back on and its scoring starts
again from that moment, so the old failures do not switch it off again. A failed ticket breaks the streak. If a
corrected run history no longer exceeds the threshold, or all tools recorded as missing are now present on an
assigned online machine, the role returns immediately and does not wait for probation.
The `master` cycle has no ticket to try, so a switched-off master role is probed with triage tickets.

## Command Center

The **AI quality by role** panel (Command Center, below *AI accounts*) lists every account and role with:

- state: On, Off with a probation ticket available, or Off with a probation ticket running, plus the note;
- **success rate**: `1 − failure score` over the latest `window` scored runs;
- **cost per finished ticket**: recorded run cost (`runs.cost_usd`, all runs of the role) in the last 30 days divided
  by the tickets that role finished successfully in that time;
- **rework rounds** in the last 30 days: dev runs sent back by verification, or verdicts of "send back" by a verifier.

The same data is in `GET /v1/role-health` and under `role_health` in `GET /v1/state`. `POST /v1/work/claim` checks
whether a role is enabled and offers its work or eligible probation ticket; `POST /v1/runs/{run_id}/result` records
the fenced result and re-scores the role pool, including evidence-backed tool-missing failures.

**Console screens:** `cc`, `machines`

## Retry after a clean no-change run

A dev run that ends with a clean "nothing to change" result is stored as `no_changes`. Role health never scores it as
a failure, but it counts toward the retry backoff so the ticket is not offered again at once. The master action
`POST /v1/master/actions` with `retry` therefore accepts a latest run with outcome `no_changes` as well as `failed`,
`timeout`, `lost`, `quota` and `blocked`.

## Thresholds

Defaults: window 10 runs, failure score above 60 %. An owner (permission `console.manage`) edits both on the panel;
`PUT /v1/role-health/settings` with `{"window_runs": 1-100, "failure_percent": 0-99}` stores them in
`role_health_settings`, records the change in `write_audit` and re-scores every role at once.

## Timeouts and lost leases

A run stopped by the time limit or by a lost lease counts as a quarter of a failed run in the failure score. The size of the ticket and the load of the machine are often the cause, so ten such runs alone never switch a role off, while real failures still do.

