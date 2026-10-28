# Load-aware scheduling

**Console screens:** `machines`, `cc`

AI Flow uses node heartbeat measurements to decide whether a machine can receive another work offer. The rule is
deterministic and runs in the v4 control plane. A machine's current runs keep going when its load is high; the
control plane only stops new claims.

## Limits on Machines and AI

Open **Machines and AI**, select a machine, and edit **Load-aware offers** in its details. The defaults are:

- short-term load (`load1`) below `0.85` per reported CPU;
- memory in use below `85%` of reported memory;
- free disk above `10%` of reported disk.

The same screen lets an owner set a host CPU load limit. The hostname reported in heartbeats groups machines that
share a host, so one host limit applies to every machine with that hostname. Its default is also `0.85` load per
CPU. The host run ceiling is the larger of the current active-run count and the CPU budget left after subtracting
estimated non-worker load: `floor(limit × reported CPUs − max(0, load1 − active runs at the heartbeat))`, but it
never falls below runs already active. This is a scheduling estimate based on the latest heartbeat, not a promise
that every run consumes exactly one CPU or that the operating system load is reserved. New claims count active
runs again while holding a shared-host database lock, so simultaneous claims from different machines cannot each
use the same remaining budget.

At or above a machine or host limit, new work is held. The machine card and details show the cause, measured value
and observation time. Missing CPU, memory or disk measurements also hold new offers and identify the missing
telemetry; the existing runs continue. Work resumes automatically once the cause clears the recovery margin: CPU
load must be at least `0.05` below its limit; memory must be at least 5 percentage points below its limit; free
disk must be more than 5 percentage points above its limit. The displayed measurement time advances with each
heartbeat.

Between 70% of the CPU limit and the limit, the control plane offers at most half of a machine's currently free
worker slots. The host also has a shared concurrent-claim ceiling based on its configured CPU limit and reported
CPU count, so machines on one host cannot each use a full machine-sized budget. Other readiness, quota, project,
role and account capacity checks still apply.

If a run reports failure or timeout while a machine or its host is currently over a hard limit, the run is stored
as retryable `load_shed` with the causing measurements. Role health excludes it from failure scores and the ticket
does not enter failure backoff, so an eligible machine with room can claim it as soon as the lease is released.

## Metrics

State changes emit `machine.load_throttled`, `host.load_throttled`, `machine.load_resumed` and
`host.load_resumed` events. Throttling event rows are kept in the control database and shipped through the normal
OpenObserve event stream. The **Throughput and load throttling** panel on the AI Flow events dashboard plots
finished runs, throttle transitions and the measured load per CPU alongside one another.

The control plane reads `load1`, `cpus`, `mem_used`, `mem_total`, `disk_free_pct` (or `disk_free` and
`disk_total`) and `hostname` from the existing node heartbeat. No model or AI decision is involved.

## Control API

The generated API reference documents these v4 contracts:

- `PUT /v1/machines/{name}/load-limits` saves machine CPU, memory and disk thresholds plus an optional shared-host
  CPU threshold. It requires a signed-in user with machine-management permission. A missing machine returns 404;
  invalid or out-of-range limits return 422.
- `POST /v1/nodes/{name}/heartbeat` records the latest reported load measurements and recalculates machine and
  host state. Omitting the whole `metrics` field preserves older agents' previous inventory; sending a partial
  `metrics` object replaces it, and any required measurement absent from that object holds new work.
- `GET /v1/state` returns `load_limits`, `load_state`, `host_load_limits` and `host_load_state` on each machine.
- `GET /v1/deploy/state` includes the same machine load fields for deploy tooling.
- `GET /v1/work/candidates`, `GET /v1/master/candidate` and `GET /v1/version-releases/candidates` suppress work
  offers when machine or host capacity is held or full. `POST /v1/work/claim` and
  `POST /v1/version-releases/claim` repeat the capacity check before inserting a run.
- `POST /v1/runs/{run_id}/result` records a failure or timeout during a current hard-limit violation as retryable
  load-shed work, preserving the measurements that caused the retry.

The Machines and AI screen is covered by the [screen guide](../screens/machines.md).
