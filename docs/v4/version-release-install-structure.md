# Version release routes structure (JAIF-549)

`install(app, ctx, deps)` in `aiflow/v4/control/features/version_release/routes.py` calls two route-group installers in the original order: `_install_offer_and_claim_routes` (what a machine is offered and the claim) and `_install_completion_routes` (completing a release run and the post-merge results). The bodies are steps in three modules that call the policy modules directly, so the documentation check still sees which entitlement, quota and release rules each route goes through:

| Module | Steps |
|---|---|
| `candidate_steps.py` | `early_refusal` (blocked offer, licence, version skew, machine and account state), `offered_versions` (readiness, container runtime, peer quota, per-project entitlement) |
| `claim_steps.py` | the checks before a claim, then `start_release_run` (lease, branch, run rows, audit; the lease is released on failure), `announce_claim` and `claim_response` |
| `completion_steps.py` | `release_run_and_version`, `completion_report`, `release_gateway`, `complete_release`, `record_post_merge` |

`tests/v4/test_version_release_claim_parity.py` pins 36 claim cases recorded from the unchanged handler; the registrations of every install are pinned by `tests/v4/test_route_install_registrations.py`.

## Routes

`GET /v1/version-releases/candidates`, `POST /v1/version-releases/claim`, `POST /v1/version-releases/{version_id}/complete` and `POST /v1/environment/post-merge/complete`.
