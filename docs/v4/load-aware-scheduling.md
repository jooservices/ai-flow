# Load-aware scheduling

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
CPU. A host's available claim budget is calculated from the limit, CPU count, recent load and active runs observed
at the latest heartbeat, and is rechecked while claims are inserted.

At or above a machine or host limit, new work is held. The machine card and details show the cause, measured value
and observation time. Work resumes automatically once the cause clears the recovery margin: CPU load must be at
least `0.05` below its limit; memory must be at least 5 percentage points below its limit; free disk must be more
than 5 percentage points above its limit. The displayed measurement time advances with each heartbeat.

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
