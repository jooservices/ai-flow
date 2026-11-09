# Test-suite slots on each machine

**Console screens:** `machines`, `cc`

The Machines and AI screen sets how many worker-run test suites may execute on each machine at once. The default is
two; owners can choose from one to 32. A suite that reaches the limit waits for a slot. Test processes start at nice
10 so the AI tools and node agent keep their normal scheduling priority.

## Steps

The Machines drawer saves the setting with the machine's load limits. The control plane validates and persists it,
includes it in the machine state, and returns it in the node's desired heartbeat state. The node agent writes the
limit atomically to `/run/aiflow4-test-slots` before it reconciles workers. That root-owned directory grants the
`aiflow-worker` group access to its locks while keeping the setting file read-only to workers.

`worker.suite_slots.suite_slot` takes a process-shared file lock and waits until fewer suites than the configured limit
are active. `gate_step.run`, `post_merge.run_post_merge`, and `version_release._run_check` hold a slot across each
suite. They use `worker.suite_slots.nice_prefix` when starting check processes; child test processes inherit nice 10.
The lock is released when the check finishes or its worker exits.

To add another worker-run check path, wrap the test process and its wait/collection in `suite_slot()`, and prefix its
process arguments with `nice_prefix(run_as)`. Keep setup outside the slot when it does not start the check process.

## Storage

`nodes.test_suite_slots` stores one bounded `smallint` per machine. The primary key `nodes.name` serves the state and
heartbeat reads and the settings update, so the column needs no separate index. A `NOT NULL` default of two and a
check constraint from one through 32 cover existing and new nodes. The value adds two bytes per node, shares the
existing node-row lifetime, and has no history table or cleanup job. The migration only adds the column; earlier
releases ignore it, and their load-limit updates leave it intact.

## Routes

- `PUT /v1/machines/{name}/load-limits` saves machine load thresholds and the configured test-suite slot count.
- `POST /v1/nodes/{name}/heartbeat` records node telemetry and returns the desired test-suite slot count.
- `GET /v1/state` returns each machine's test-suite slot count with its load limits.
- `GET /v1/deploy/state` returns the same per-machine test-suite slot count for deploy tooling.
