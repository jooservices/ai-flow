# Small tickets: 15 minutes normally, a warning at 30 and a hard stop at 45

A development ticket must be small enough for one short AI run. `core/work_limits.py` holds the defaults: **target 15,
warn 30, hard 45 minutes**. A project overrides any of them with `limits: {work_minutes: {target, warn, hard}}` in its
control-repo file, and every part of the flow reads the same values (watchdog clocks, run time limits, the Dashboard).

- **Size rule:** `decide_split` marks a ticket `needs_split` when its size label is above the target (XS 5, S 15, M 30,
  L 60, XL 120 nominal minutes) and there is no run history yet; with history, the estimated high minutes above the
  target decide. A ticket that needs a split goes through the split plan before a developer is offered it.
- `POST /v1/work/claim` returns the project's limits to the worker, which uses the hard limit as the run's time limit.
- Splitting after a timeout and the Dashboard timeout share belong to JAIF-335.

The limits also appear in `GET /v1/master/context` and `POST /v1/master/actions` (the master AI's view of running clocks) and in
the defaults used by `POST /v1/projects` when a project is created without its own `work_minutes`.
