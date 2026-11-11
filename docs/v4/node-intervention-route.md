# Node operator intervention route

The node agent reports operator actions to Flow Doctor through the `node_interventions` router. The route registry
discovers this module under `aiflow/v4/control/routes`; the fleet installer does not register it. The handler reads
the application context from the request, authenticates the bearer against the node named in the path, and delegates
recording to `flow_doctor.record_operator_intervention`.

To add a related endpoint, define its request model with the owning feature, add a handler to a module-level
`APIRouter` under `control/routes`, and use `route_deps` for request context and shared authentication. The registry
mounts each router module automatically.

## Routes

- `POST /v1/nodes/{name}/operator-interventions` Records node-reported operator evidence for Flow Doctor and returns its record ID.
