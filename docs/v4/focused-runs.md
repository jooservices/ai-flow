# Focused developer runs and reused pipeline checks

A developer run does not repeat the full v4 suite. After the commit, its local gate takes the files the branch changed since the base and runs the tests that cover them (the reviewed map in `docs/v4/affected-tests.md`). Changes to shared code, an unmapped file or an unreadable change list keep the full suite. The scale proof runs only when its own test is selected. The pull request pipeline runs the full suite once. The selected tests are recorded in the run details as `focused_tests`. Projects with their own check commands run them as configured.

Verification does not repeat what the pipeline already proved. Before its local gate, it reads the checks of the pull request head commit; a gate step whose pipeline job has checks and all of them passed is skipped, and its name is recorded in the run details as `reused_pipeline_checks`. Steps without a pipeline job (such as the commit header rules) and jobs that are red, pending or missing still run. The CI result is then collected as before.

To add a step that can be reused, give it the pipeline job key in `aiflow/v4/core/local_gate.py` and list the job in `aiflow/v4/core/pipeline_checks.py`.
