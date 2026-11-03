# Work route install structure

`features/work_routes/routes.py:install` builds the shared work helpers and installs route groups in their existing registration order. The frozen `WorkRouteRuntime` carries the app context, dependencies and master-cycle settings used by those helpers.

## Install order

1. `install_candidate_routes` in `routes_candidates.py` registers worker and master candidate routes.
2. `install_claim_routes` in `routes_claim.py` registers the work claim handler and returns it with the shared helpers.

## Routes

### `install_candidate_routes`

- `GET /v1/work/candidates`
- `GET /v1/master/candidate`

### `install_claim_routes`

- `POST /v1/work/claim`

## Adding a route group

Add a sibling `routes_<group>.py` module with an `install_<group>_routes` function. Pass the context and only the helpers the group needs, then call it from `routes.install` in registration order. Keep each route decorator beside its handler and list its method and path under that installer here.
