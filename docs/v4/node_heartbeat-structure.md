# Node heartbeat structure

`node_heartbeat` in `aiflow/v4/control/features/fleet/routes.py` keeps the HTTP contract and calls the phases in
`aiflow/v4/control/features/fleet/heartbeat.py` in order.

1. `update_node_heartbeat_row` stores capacity, reported inventory, sanitized runtime settings and heartbeat time.
2. `persist_heartbeat_credentials` updates machine and account credential expiry only for accounts assigned to this node.
3. The route records the slot pressure and publishes `node.heartbeat`; when an accepting node returns from a non-online
   state, it also publishes `node.online` and closes its node-down incident.
4. `merge_worker_reports` keeps each account's highest-ranked reported state and counts its instances. Empty account IDs
   are ignored.
5. `persist_worker_reports` stores worker state on account and machine rows while retaining the existing signed-out,
   cooldown, unavailable-probe and working-to-idle status rules.
6. `desired_heartbeat_state` returns eligible accounts and worker IDs, queued and shared sign-ins, the protocol-gated
   release target, project details, runtime-setting bootstrap, pending probes, tools and versions. It also refreshes
   heartbeat readiness before building the runtime-setting bootstrap and probe response.

## Routes

`POST /v1/nodes/{name}/heartbeat` authenticates the node, refuses a path name that differs from the authenticated node,
then records the heartbeat and returns the node's desired state. Event publishing remains an explicit call in this route.

To add a phase, give it a named function in `heartbeat.py`, call it explicitly at the corresponding point in
`node_heartbeat`, and add characterization coverage to `tests/v4/test_node_heartbeat_parity.py` and its JSONL fixture.
