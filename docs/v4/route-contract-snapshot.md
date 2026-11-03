# Route contract snapshot (JAIF-285)

`tests/v4/fixtures/route-contract-snapshot.jsonl` records, for every v4 API route, its HTTP method, path, authentication requirement and response model, one JSON line per route and method. `tests/v4/test_route_contract_snapshot.py` builds the live application, derives the same four details and compares them with the snapshot; on a mismatch it fails with a readable unified difference (`-` is the snapshot, `+` is the live route).

- **auth**: `public` (open paths and prefixes of the sign-in gate), `node-token`, `owner-token`, `reviewer-token` (a named token dependency, always together with `bearer-header`), `bearer-header` (a handler that reads the `Authorization` header itself), `bearer-checked` (a handler marked with `checks_bearer`), or `console-session` (the signed-in console session).
- **response**: the declared response model, otherwise the return annotation of the handler (`dict`, `JSONResponse`, ...), or `unannotated`.

To accept an intended change, run `AIFLOW4_UPDATE_ROUTE_SNAPSHOT=1 uv run --frozen pytest tests/v4/test_route_contract_snapshot.py` and commit the changed snapshot, so the change is visible in review. The older `application-context-route-snapshot.json` (method and path only, plus the screen list) stays as it is.
