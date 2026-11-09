# Canary maximum age and run count overrides

Set `AIFLOW4_CANARY_MAX_AGE_S` or `AIFLOW4_CANARY_MIN_RUNS` in `/etc/aiflow/deploy.env` to override the matching
canary setting. The release and develop deploy channels pass each nonempty value into the promotion decision. When
either variable is unset, that threshold continues to come from the control-plane settings.

## Steps

1. `load_canary_settings` reads the minimum age, minimum runs and maximum age from the control-plane state. An
   explicitly set environment value keeps precedence for maximum age and minimum runs.
2. `canary_cli` forwards those two environment values to the evaluation process. `run_canary` overlays nonempty
   values on the stored thresholds and leaves all other stored thresholds intact.
3. Under-observed promotion records use the same effective minimum run count.

## How to extend

For another environment override, preserve the settings value when the variable is empty, forward the environment
value in both deploy channels, and overlay only that field in `run_canary`. Add a decision test for the set and unset
cases and channel tests for release and develop.
