# Scheduled account quota refresh

**Console screens:** `settings`

AI Flow stores per-run token and cost totals from the worker result. A run may also carry a provider-reported quota
snapshot; that snapshot is stored separately from the run's token totals. When a run has no quota snapshot, it does not
create a quota sample from the account's previous snapshot.

Workers periodically claim one due refresh per logical account from the control API. The control database owns the
interval and the account-level lease, so workers on multiple machines cannot refresh the same account concurrently.
The interval is configurable from **Settings → Account quota refresh**, is stored in the database, and defaults to
15 minutes. A worker background scheduler polls the database every 30 seconds, independently from task assignment and
execution. The first refresh is due as soon as a worker polls. A failed refresh records its reason and waits for the
configured interval before retrying.

Managed provider sign-in completion updates authentication health and schedules a fresh quota read immediately.
Authentication health does not depend on whether a provider CLI can return quota; a failed or unsupported quota read
retains the prior snapshot and reports the read error. A successful quota read updates quota separately. Quota refreshes
do not clear task quota cooldowns or rewrite general account health; those continue to follow task results and sign-in
flows.

Refresh uses a fixed, provider-owned read-only CLI status command. Codex uses `/status`; Claude Code uses `/usage`.
AI Flow starts the provider TUI only to issue that command and never falls back to a natural-language prompt. The
current adapters for Grok and OpenCode do not expose a supported quota-only command, so those accounts keep their last
provider snapshot and show an unsupported/parse error in Settings until an adapter is added. Their successful
provider login command still records authentication health so managed sign-in can finish; it does not create a quota
snapshot. GitHub Copilot is not a configured AI Flow provider. CLI output is parsed only when it contains an
explicitly labeled quota window and
percentage; ambiguous output is treated as a failed refresh, and the existing snapshot is retained.

Every accepted snapshot records its source and observation time. Run quota observations use source `run`; scheduled
CLI reads use source `cli`. A scheduled read uses the helper's observation time, clamped between the server-side claim
and result-receipt times; if the helper omits an observation time, the control plane uses the receipt time. If a task
reports a newer snapshot while an earlier refresh is still running, that refresh is retained as an older sample but
cannot replace the task's account snapshot. Forecasting uses provider quota samples when available and continues to
estimate burn from completed run history; run usage is not added to provider-reported utilization.

On upgrade, account quota values without stored provenance are treated as unknown until a new read arrives. Historical
quota samples without source metadata are retained for audit but excluded from provider sample-rate/reset learning;
recorded task usage remains available for estimate-based forecasts.

For accounts configured with multiple concurrent sessions, the control API records each development run's estimated
upper-bound token reservation atomically with the run claim under the account lock. Candidate assignment subtracts live
reservations, unresolved estimates for runs that lost their lease, and actual tokens from completed runs newer than the
latest provider snapshot. A provider snapshot newer than a lost run reconciles and releases its estimate; a recorded
result replaces it with reported usage. Existing lost runs without an end time are anchored at upgrade time so an
older snapshot cannot release their reservation. Single-session accounts keep their existing serialized claim path.

The console API is `GET /v1/quota-refresh/settings` and `PUT /v1/quota-refresh/settings`. Both return `settings` (including the interval and
update metadata) plus the per-account `jobs` status list. `PUT` accepts `{"interval_minutes": 15}`; the allowed range
is 1–1440 minutes. These endpoints require `console.manage`.

Workers call `POST /v1/accounts/{account_id}/quota-refresh/claim` and
`POST /v1/accounts/{account_id}/quota-refresh/result` with node authentication. A claim returns either `{"job": null}`
or a job containing its account, provider, lease token, and claim time. A result accepts the lease token, success flag,
provider quota windows, optional observation time, error, and model. It returns `{"recorded": true}`; a result may be
accepted after the lease's nominal expiry if no later claim has replaced its token. Once superseded, the old token
returns HTTP 409. The server sets the sample source to `cli` and validates successful quota data against the account's
provider.

## Related control API contracts

These existing contracts keep their route and response shape. Their quota-related behavior now uses only trusted
provider observations; task usage remains separate from provider-reported utilization.

- `GET /v1/capacity` and `GET /v1/dashboard/quota` return capacity and quota views based on the latest trusted
  snapshot. Capacity assignment also accounts for active token reservations and measured usage after that snapshot.
- `GET /v1/work/candidates` retains its response contract and selects work using the latest trusted quota snapshot,
  live reservations, unresolved estimates for lost runs, and actual usage newer than the snapshot.
- `GET /v1/master/autonomy`, `GET /v1/master/report`, `GET /v1/state`, and `GET /v1/deploy/state` retain their
  existing response contracts; quota fields without trusted provenance are shown as unknown.
- `POST /v1/work/claim` atomically records an estimated upper-bound reservation for concurrent development runs.
- `POST /v1/runs/{run_id}/heartbeat` records when a worker loses its run lease; its estimated token reservation
  remains in capacity accounting until a newer provider quota snapshot reconciles it.
- `POST /v1/runs/{run_id}/result` records the task's token and cost totals independently. It updates provider quota
  only when the worker reports a fresh quota observation; it never copies an older account snapshot into a new sample.
- `POST /v1/accounts/{account_id}/probe` remains available for legacy worker checks with its existing request shape.
- `POST /v1/accounts/{account_id}/sign-in/status` reports managed sign-in state, updates authentication health, and
  queues an immediate refresh when that flow completes. `POST /v1/accounts/{account_id}/signin-complete` reports
  local shared sign-in and updates auth health only; it does not create quota data.
- `POST /v1/version-releases/claim` keeps its existing version-release claim contract and remains independent of
  quota refresh scheduling.
