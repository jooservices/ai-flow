# Business routes structure (JAIF-550)

`install(app, ctx, deps)` in `aiflow/v4/control/features/business/routes.py` is a short function: it installs the business and stakeholder route groups, then registers the console state and deploy routes itself (the state route keeps its response schema in `routes.py`, where the documentation tests look for it).

| Part | Module | Owns |
|---|---|---|
| Business view | `routes_business.py` | change requests, release readiness and progress per project |
| Stakeholder | `routes_stakeholder.py`, `stakeholder_steps.py` | the stakeholder page and the verdict: `check_verdict`, then `record_acceptance` or `file_uat_feedback` |
| State | `state_view.py` | the cached snapshot (`cached_snapshot`), the cut to a person's projects (`restrict_to_projects`) and to their role (`hide_machines_and_detail`) |

## Routes

`GET /v1/business/{key}/change-requests`, `GET /v1/business/{key}/release-readiness`, `GET /v1/business/{key}/release-progress`, `POST /v1/business/{key}/change-requests`, `GET /v1/stakeholder/{key}`, `POST /v1/stakeholder/{key}/verdict`, `GET /v1/state`, `GET /v1/deploy/state` and `GET /v1/deploy/ci-state`.
