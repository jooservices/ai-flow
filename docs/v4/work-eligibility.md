# Work eligibility pipeline

`GET /v1/work/candidates` and `POST /v1/work/claim` use the same ordered eligibility pipeline. Claim revalidation can
scope that pipeline to one ticket and step while retaining the same checks and filters.

## Stages

1. **Account rules** are `account_may_work` in `aiflow/v4/control/features/work/eligibility_scope.py`. They require a
   known account assigned to the node, an unpaused account, no cooling period, and available shared-account sessions.
2. **Node rules** are `online_node_row` and `node_accepts_work` in the same file. They require a fresh online heartbeat
   and an agent that accepts work.
3. **Project rules** are `open_projects` in the same file. It filters paused projects, parallel limits, missing pinned
   tools, unhealthy declared services, and blocking readiness results. Ticket-specific container requirements are
   checked by `Scope.needs_missing_container_runtime`.
4. **Role and lease rules** are assembled in `eligible.py` and `Scope`. They select the requested account roles, apply
   role health and probation, and load held lease keys.
5. **Candidate collection** lives in `eligibility_candidates.py`: `verify_candidates`, `post_merge_candidates`,
   `triage_candidates`, `intake_candidates`, `impact_candidates`, and `dev_candidates`. Development also uses
   `HotFileGate` to cache footprints and preserve hot-file reservations.
6. **Ordering and routing** are composed by `eligible()` and applied by `finalize()` in `eligible.py`. They preserve
   project priority and age, Flow doctor exclusions, flow-bug priority, provider headroom, quota peers, and capacity
   routing.

Add each new eligibility rule as a named function with a one-line docstring describing what it holds back, and add a
focused test in `tests/v4/test_eligibility_rules.py`. Keep the SQL, filter sequence, result ordering, side effects, and
exceptions stable when changing this pipeline.
