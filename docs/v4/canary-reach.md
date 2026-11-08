# Canary reach: merged fixes go live quickly

**Console screens:** `cc`, Settings (`settings`)

A merged change must reach the running control plane within about 15 minutes in normal operation. The
automatic canary no longer restarts when a newer merge arrives, thresholds live on the settings screen, and
the Dashboard shows how far behind the running build is.

## Steps

1. **Keep the canary under test.** When `CANARY.json` already names a candidate, a newer merge is recorded as
   pending and the observation continues. The flow finishes that canary, then starts the newest build
   (`keep_canary_target` in `aiflow/v4/control/canary.py`).
2. **Read thresholds from settings.** Minimum time, minimum completed runs and maximum time default to
   5 minutes, 3 runs and 15 minutes. Owners edit them on the Settings screen (`/v1/canary/settings`). The
   auto-deployer loads those values from `/v1/deploy/state` (`control_canary.settings`) and passes
   `--from-settings` to `aiflow4 canary evaluate`.
3. **Judge idle canaries on health.** After the minimum time, if the canary node is healthy and has had no
   completed or running work (no work available), `decide` promotes with cause `too_few_runs` instead of
   waiting for the maximum time.
4. **Name the alert cause.** Every decision carries `cause`: `too_few_runs`, `failures` or `slow`. Rollback
   acts on failures; under-observed promotions record an informational incident that names the cause; a slow
   canary keeps waiting until thresholds are met.
5. **Show lag on the Dashboard.** The Dashboard panel “Control plane canary” shows the running build, the
   build in canary, how many merged changes are not live yet, the age of the oldest of them, and recent
   merge-to-live times (`control_canary` on `/v1/state`). The auto-deployer notes pending builds while a
   canary is under way and records merge-to-live when a promotion succeeds.

## Routes

- `GET /v1/canary/settings` returns the canary thresholds (minimum age and minimum runs) shown on the settings screen.
- `PUT /v1/canary/settings` changes those thresholds; it needs the `console.manage` permission and is audited.
- `GET /v1/canary/status` reports the running build, the candidate build and why a canary is held.
- `GET /v1/deploy/state` and `GET /v1/state` carry the same status to the Dashboard, which shows how far behind the running build is.

## How to extend

- Add a new threshold field on `canary_settings`, the Settings panel, and `Thresholds` / `decide` together.
- Prefer finishing the canary under test over restarting it; if a short batching window is needed later, keep
  that decision in `keep_canary_target` so the deployer stays thin.
