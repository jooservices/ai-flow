# Runbooks

**Assumes:** you are an Owner or Super Administrator, you can see the incident in [Command Center](../screens/cc.md)
or [SA health](../screens/sa.md), and AI Flow v4 is installed. Each page has the same three parts: symptom, cause and fix.

An incident opens once for each kind and subject while the condition holds, and closes by itself when the
condition clears. Critical and warning incidents are emailed. The list below has one page for every kind the code raises.

| Kind | Severity | In short |
|---|---|---|
| [backup_failure](backup_failure.md) | warning | The scheduled encrypted backup failed |
| [backup_restore_test](backup_restore_test.md) | warning | The weekly restore test failed |
| [flow_idle](flow_idle.md) | critical | Tickets are ready but no run started |
| [licence_limit](licence_limit.md) | critical | A licence limit stops new work |
| [config_pin](config_pin.md) | critical | A project's stack has no pin in `versions.lock` |
| [node_down](node_down.md) | critical | A node stopped sending heartbeats |
| [node_tools](node_tools.md) | warning | A node misses tools for a project or its AI accounts |
| [node_services](node_services.md) | warning | A node's test services (PostgreSQL, Redis) are unhealthy |
| [node_no_ai](node_no_ai.md) | warning | A node has no AI account |
| [master_invalid_plan](master_invalid_plan.md) | critical | The master repeatedly returned an invalid action plan |
| [ticket_stuck](ticket_stuck.md) | warning | A ticket failed three runs in a row |
| [run_slow](run_slow.md) | warning | A run passed its warning time |
| [run_hard_limit](run_hard_limit.md) | critical | A run passed its hard time limit |
| [account_quota](account_quota.md) | warning | An AI account is out of quota, signed out or failing |
| [quota_prediction](quota_prediction.md) | warning | An account may use its quota before the provider resets it |
| [role_health](role_health.md) | warning | An AI account is switched off for a role after repeated failures |
| [quota_high](quota_high.md) | warning or critical | An account's usage window is at 90% or more |
| [role_coverage](role_coverage.md) | warning or critical | A role has fewer than two ready accounts |
| [jira_auth](jira_auth.md) | warning or critical | Jira is not connected or must be reconnected |
| [version_skew](version_skew.md) | warning | A node's release does not fit the control plane |
| [fleet_rollout](fleet_rollout.md) | critical | A rolling node upgrade halted |
| [fleet_rollback](fleet_rollback.md) | warning or critical | The rollback after a halted rollout finished or failed |
| [upgrade_blocked](upgrade_blocked.md) | critical | The pre-upgrade check blocked a control upgrade |
| [release_verification](release_verification.md) | critical | A node rejected a release (checksum or signature) |
| [canary_rollback](canary_rollback.md) | critical | A canary release was rolled back |
| [canary_under_observed](canary_under_observed.md) | info | A canary was promoted with too few runs |
| [openobserve_delivery](openobserve_delivery.md) | warning | OpenObserve rejects event batches |

The control plane opens these incidents from health checks and recorded events. The master cannot open or close
incidents directly. Every kind it can raise has a page in this list.

## If the page you need is not here

Read the incident's detail first: it names the cause. The older operator notes for the v1 flow are in `docs/RUNBOOK.md`.
