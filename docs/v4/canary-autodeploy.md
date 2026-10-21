# Canary auto-deploy

`aiflow4-autodeploy` stages the newest published release, then targets one online node with assigned accounts.
Set `AIFLOW4_CANARY_NODE` in `/etc/aiflow/autodeploy.env` to pin the canary; when it is empty, the deployer picks
the first eligible node by name. The selected node receives the candidate through `CANARY.json`; `CURRENT` stays
on the previous release, so other nodes keep that version during observation.

The canary clock starts when its heartbeat reports the candidate installed. Defaults are 20 minutes minimum,
five completed runs minimum, and 60 minutes maximum. The timer checks once a minute. Override the defaults in
`/etc/aiflow/autodeploy.env`:

```ini
AIFLOW4_CANARY_MIN_AGE_S=1200
AIFLOW4_CANARY_MIN_RUNS=5
AIFLOW4_CANARY_MAX_AGE_S=3600
AIFLOW4_CANARY_FAILURE_RATIO=2
AIFLOW4_CANARY_MIN_INFRA_FAILURES=3
AIFLOW4_CANARY_NODE=
```

For each measurement, the registry counts completed runs on the candidate node since the clock started. An
infrastructure failure is a run with a nonempty `error_kind` other than `quota`, a `blocked` run with `infra`, or
a `lost` run. The baseline is completed runs from the previous release on the other nodes during the 24 hours
before the canary started. A rollback requires at least the configured number of canary infrastructure failures
and a canary failure rate at least the configured multiple of the baseline rate. A zero-run baseline has a zero
rate. A failed candidate is rejected until a newer release is published; rollback clears its node override,
opens a critical incident and creates a `flow-bug` Jira Bug with the suspected tag commit.

When the minimum time and run count are met, the deployer promotes the release to the control plane and changes
`CURRENT`; other nodes then receive it on heartbeat. At the maximum time, a candidate with fewer than the
required runs is treated as stable, and an informational incident records the low sample count before promotion.
The bug project defaults to `TESTFLOW`; set `AIFLOW4_CANARY_BUG_PROJECT` in the control service environment to
use another Jira project.

The first tagged deploy from a legacy commit id keeps the existing control-plane health-check path because that
commit cannot be restored through the signed node release protocol. Version-to-version deploys use the canary.
