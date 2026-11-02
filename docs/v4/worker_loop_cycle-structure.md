# Worker loop cycle structure

`Worker.once()` runs one poll in three ordered phases:

- `_run_version_release_cycle(worker)` claims and reports one version release when available.
- `_run_ba_chat_cycle(worker)` runs and reports one BA chat job when available.
- `_run_work_cycle(worker)` claims the first available master or work candidate and reports its run.

The first phase that handles work returns `True`; the cycle returns `False` after all phases find no work. Work steps are
selected explicitly in `_run_work_step(worker, claim, step)`. Add a new cycle phase as a named module-level function and
call it explicitly from `Worker.once()` in priority order. Keep claim refusals, exception handling, reporting, and state
publishing in their existing phase and order.
