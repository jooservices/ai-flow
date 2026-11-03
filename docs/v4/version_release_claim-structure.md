# Version release claim structure

## Routes

`POST /v1/version-releases/claim` keeps the claim response, refusal order, lease rollback, database writes, audit row and
event publication unchanged. Its route calls named phases in `aiflow/v4/control/features/version_release/claim_steps.py`.

## Steps

1. `check_claim_policy` loads shared-session capacity, checks the worker entitlement and refuses an incompatible node.
2. `check_claim_owner` loads the project and account, checks master pause and quota cooling, validates project and account
   eligibility, enforces project entitlement, then checks shared-session eligibility.
3. `check_claim_node` checks that the node accepts work, the project is assigned to it, and project readiness passes.
4. `resolve_claim_version` finds the configured unreleased Jira version, validates the requested repository, and resolves
   the project settings for that repository.
5. `check_claim_runtime` checks required container tooling, peer quota conflicts and an existing checking or released row.
6. `evaluate_claim_gate` gets the GitHub gateway, resumes a post-merge snapshot or evaluates current gates, and records a
   blocked gate unless it is a resume.
7. After the route claims a lease, `create_claim_branch` creates or reuses the normal or resumed release branch and returns
   the source and branch values used by the audit row and response.
8. `persist_claim_rows` stores run repository settings and upserts the version's checking state. The route keeps its audit
   and event calls explicit and releases the lease if branch setup, persistence or the audit fails.

Add a claim phase as a small, named function with explicit dependencies in `claim_steps.py`. Call it in the existing route
order and extend the claim parity cases with the affected refusal or side-effect sequence.
