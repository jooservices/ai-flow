# Upgrade host layout and promotion failures

When a control-plane candidate is promoted, the host must already have every secret, settings and
configuration file that a fresh install would have created. A release that adds a new required file
(for example owner-managed runtime settings) must not depend on a hand-made file on existing
installations.

## Host layout before services start

Before control containers are recreated, the upgrade path calls `ensure_runtime_layout` on
`AIFLOW4_SECRETS_DIR` (production default `/etc/aiflow/secrets`). That helper:

1. Creates the secret directories (`control`, `github`, `jira`, `console`) at mode `0700` when missing.
2. Creates `runtime.env` under that secrets directory with a header and mode `0600` when missing; existing keys stay.
3. Creates the projects directory (`AIFLOW4_PROJECT_ENV_DIR`, or the `projects` sibling of the secrets directory;
   production default `/etc/aiflow/projects`) when missing.

Fresh installs reach the same helper through the secrets step. Services that read optional env files treat a
missing file as empty (no keys), so an empty default file is enough for compose mounts and for the Settings
screen.

To add another required host file later: extend `ensure_runtime_layout` with the path, safe mode and default
contents, call it from the same upgrade and install paths, and cover an old layout in the upgrade tests.

## Failed promotion alerts and retries

After a canary reports the candidate stable, promotion still has to deploy the control plane. When that
deploy or the following health check fails:

- A **CRITICAL** alert names the failed step, the error (for example `missing file /etc/aiflow/secrets/runtime.env`),
  the stuck build, and how many merged changes are not live yet. It is separate from a canary under-observed note.
- Each failure is recorded in `PROMOTION_FAILURES.json` under the releases directory.
- After **two** failures of the **same build** with the **same cause**, that build is written to
  `CANARY_REJECTED` and is not retried.
- The next build is tried only when the recorded cause has changed (a missing-file cause clears once that file
  exists, which the layout step above normally does before the next attempt).

Successful promotion clears the failure record.
