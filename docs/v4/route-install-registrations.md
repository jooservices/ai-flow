# Route install registrations (JAIF-547 to 558)

`tests/v4/test_route_install_registrations.py` runs the `install(app, ctx, deps)` function of every feature in `aiflow/v4/control/features/*/routes.py` against a recording fake app and mock dependencies. For every registration it records the HTTP method, path, handler name, handler signature and decorator options, and compares the list with `tests/v4/fixtures/route-install-registrations.jsonl` (148 registrations in 14 features).

Use it when an `install` function is split into route groups: the split must leave this list unchanged (same routes, same order). It complements the route contract snapshot (`route-contract-snapshot.md`), which records what the whole application serves, and the per-ticket parity tests, which pin behaviour.

To accept an intended change, run `AIFLOW4_UPDATE_ROUTE_SNAPSHOT=1 uv run --frozen pytest tests/v4/test_route_install_registrations.py` and commit the changed file so the change is visible in review.
