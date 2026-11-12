# State snapshot profile

This page records the JAIF-436 profile of `GET /v1/state` on the default scale-proof data. It is evidence for the follow-up optimization work; it does not claim the 1.5 s console target or ten passing local scale-proof runs.

## Reproduction and measurement method

The scenario is `scale_harness.Scenario()` and `scale_harness.run()`: 100 projects, 50 machines, 100 accounts, 200 simulated sessions, 2,000 history tickets, and traffic waves of 200, 80 and 80 tickets. It uses an isolated PostgreSQL scratch schema and Redis prefix through `scale_harness.scratch()`, with the existing test-service wrapper. No AI provider or external integration is called.

The one-off profiler wrapped `features.business.state_view.cached_snapshot`, `control.state.snapshot`, the existing harness request counter, and the database `all`/`one`/`run` helpers. It measured full `/v1/state` request wall time, cache-call wall time, build wall and thread CPU time, lock acquire wait, statements and rows returned. It recorded only numeric counts and function names; it did not retain SQL text or parameters, row values, response bodies, or identifiers. The cache's `include_signin` value was counted as a separate visibility class. Coalesced returns were classified by whether `state.snapshot` ran for that request. The temporary lock proxy delegates to the original lock and is restored with all other patches in `finally`.

At most 20 state-request samples and three function profiles per category were retained. Function contributors below are self thread-CPU time aggregated from three burst builds, so nested function time is not double-counted. Idle reads were three sequential `/v1/state` calls after traffic settled, using the same application database and seeded scenario. The uninstrumented comparison was a separate run with the same scenario seed and default budgets. Separate runs can vary with host and database cache state.

To rerun the proof through the approved local test environment:

```sh
/home/joos/Workspaces/.tmp/test-env/run.sh uv run --frozen pytest tests/v4/test_scale_proof.py -q -p no:cacheprovider
```

The profiling driver used for this report is temporary (`.tmp/profile_state_snapshot.py`) and is not part of the application. Re-run it only through `/home/joos/Workspaces/.tmp/test-env/run.sh`; its JSON output is sanitized and written under `.tmp/`.

## Host and proof results

| Measurement | Uninstrumented proof | Profiled proof |
|---|---:|---:|
| Host | Linux x86_64, CPython 3.12.15, 24 reported CPUs | Same host |
| Scenario | 100 projects, 50 machines, 100 accounts, 200 sessions | Same default scenario |
| History / traffic tickets | 2,000 / 360 | 2,000 / 360 |
| Traffic outcomes | 344 pickups, 16 held, 3 blocked | 344 pickups, 16 held, 3 blocked |
| Peak active runs | 171 | 173 |
| Peak in-flight app requests | 32 | 32 |
| Console/worker overlap | Passed | Passed |
| Worst console-view p95 (diagnostic) | 3.748 s | 4.572 s |
| `/v1/state` maximum per-view p95 | 3,744 ms | 4,529 ms |
| Project picker p95 | 250.349 ms, proof failed this budget | 59.504 ms |
| Ticket pickup p95 | 57.029 s, proof failed this budget | 63.445 s, proof failed this budget |
| Overall proof result | **Failed**: `project_picker`, `ticket_pickup` | **Failed**: `ticket_pickup` |

The console seconds measurement is diagnostic in the existing scale proof. The proof failures above are reported as observed; no budget or fixture was changed. The profiled run passed its correctness checks, including overlap and active-run thresholds. It failed `ticket_pickup` at 63.445 s against its effective 31.8 s limit. Each proof seeded the same data: 2,360 tickets and 1,814 run rows remained after traffic, with zero running runs. At the first wave's measured snapshot, the ticket list contained 2,200 tickets; later waves added the remaining 160.

The harness route timing is `/v1/state` p95 per view, three calls per view, so each per-view p95 is its maximum. In the final profiled run, the request sample cap retained 20 burst calls from 40 observed while worker claim sessions were active; their wall times ranged from 1,789 to 3,504 ms. The profiled proof took 194.0 s; the uninstrumented proof took 183.4 s. These separate runs suggest overhead, but do not isolate it from host and cache variation. An earlier profiler pass also ran the proof and failed `ticket_pickup` at 106.727 s; its function timings were excluded because they did not reconcile with measured thread CPU. A later corrected profile-only run failed the same budget at 55.509 s. This run-to-run spread limits overhead conclusions.

## Snapshot request and build measurements

| Category | Requests observed | Builds / coalesced hits | Retained samples | Request wall | Cache call wall | Build wall | Build thread CPU | Lock wait | SQL statements / returned rows |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Claim-overlap burst | 40 | 20 / 20 | 20 | 1,789–3,504 ms (median 2,427 ms) | 1,668–3,426 ms | 1,952–3,424 ms | 159–942 ms | 0.001–2,949 ms (median 834 ms) | build samples: 53 / 4,340–4,636; hit samples: 1 / 1 |
| Settled idle | 3 | 3 / 0 | 3 | 1,055–1,194 ms (median 1,192 ms) | 1,040–1,177 ms | 1,037–1,174 ms | 917–1,044 ms | about 0.001 ms | 63–64 / 5,396–5,397 |

All measured requests used `include_signin=true`, matching the harness's administrator session. The visibility class remains separate in the instrumentation and its fake-based tests cover both true and false classes. A coalesced hit shares the cache result only when the existing cache permits it; later idle reads rebuilt each time, preserving fresh reads. Per-request statement and returned-row counts include the route's own authorization lookup. Build samples during the burst showed the snapshot data growing from 2,200 tickets and 0–96 active runs as the claim wave progressed; the scale harness independently observed a peak of 171–172 active runs.

The `SELECT 1` service probe measured database latency at 1.327–1.874 ms (five samples; median 1.541 ms). Redis `PING` measured 0.458–0.802 ms (five samples; median 0.575 ms). These probes are service baselines, not timings for the snapshot's individual SQL statements.

### Largest measured snapshot contributors

| Function | Module | Self thread CPU across three burst builds |
|---|---|---:|
| `loads` | JSON `__init__.py` | 405.502 ms |
| `decode` | JSON `decoder.py` | 378.987 ms |
| `machines` | `controlrepo.py` | 347.161 ms |
| `detect_encoding` | JSON `__init__.py` | 323.493 ms |
| `classify` | `role_health.py` | 301.490 ms |
| `epic_for_ticket` | `controlrepo.py` | 208.811 ms |
| `dict_row_` | psycopg `rows.py` | 199.161 ms |
| `_exec_command` | psycopg `_connection_base.py` | 182.982 ms |
| `runs_on` | `controlrepo.py` | 145.038 ms |
| `_maybe_prepare_gen` | psycopg `_cursor_base.py` | 87.416 ms |
| `_epic_key` | `controlrepo.py` | 65.401 ms |

## Interpretation and limitations

The measurements show lock queueing: some requests spent up to 2.95 s acquiring the snapshot lock, while sampled builds used 0.16–0.94 s of thread CPU and took 1.95–3.42 s wall time. Idle builds took 1.04–1.17 s wall and 0.92–1.04 s thread CPU. This supports lock wait as a direct contributor to request wall time during the measured burst, and shows that burst build elapsed time is not equivalent to CPU time. The function profile points to machine/config decoding, ticket-to-epic mapping, database cursor work, and role-health classification as measurable CPU contributors.

The profile does not time each SQL statement separately, and the five `SELECT 1` probes do not represent the snapshot's multi-query workload. It therefore cannot distinguish database execution or I/O wait from process scheduling and CPU contention within the slower build wall time. The role-health summary ran inside the snapshot profile; its cost is represented in function-level results when it ranks among the largest contributors. The profile also runs in-process on one local host and is not CI hardware, so it cannot establish the console p95 target.

The profiler leaves production code unchanged. Its unit tests exercised both sign-in visibility classes, build and shared-hit accounting, a database exception, lock timeout, restoration, and a later successful read. The existing parity/freshness and full v4 tests are run separately; see the PR body for their exact outcomes. No schema or fixture changes were made, and no TTL cache was added.
