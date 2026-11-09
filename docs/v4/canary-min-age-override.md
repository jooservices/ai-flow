# Canary minimum age override

Set `AIFLOW4_CANARY_MIN_AGE_S` in `/etc/aiflow/deploy.env` to set the minimum time before a canary can be promoted.
When set, the release and develop deployers pass this value to the evaluator, and it takes precedence over the
control-plane minimum age setting. When unset, the control-plane setting remains in effect. The value must be a
nonnegative integer that does not exceed the maximum age in control-plane settings.

## Evaluation path

1. `aiflow4-autodeploy` and `aiflow4-autodeploy-develop` forward the optional environment value across `docker exec`.
2. `run_canary` in `aiflow/v4/cli.py` applies an explicit minimum age over `thresholds_from_settings`; otherwise it
   keeps the settings value.

When adding another canary deployer, forward the environment value only when it is set, and add a test that gives the
environment and control-plane settings different values. This keeps the settings path intact when no override is
configured.
