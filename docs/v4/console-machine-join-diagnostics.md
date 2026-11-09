# Console machine join diagnostics

The administrator browser journey reads the Machine name, Machine role and Machine project fields immediately before
it submits **Show one-time command**. It then checks the captured `POST /v1/nodes/{name}/join-command` for the exact
pathname, the expected name, role and project in its JSON body, and HTTP 200. The existing visible **Copy command**
check remains in place.

When a field, path, submitted value or status fails, the journey reports its light, dark or phone mode, the status when
available, and the names of the failed checks. Diagnostics never include response content, submitted values,
commands, tokens, cookies, headers or request bodies. The test uses a pure helper in
`tests/v4/browser_e2e/machine_join_diagnostics.cjs`; its Node test is run by
`tests/v4/test_machine_join_diagnostics.py` and covers valid requests, mismatched fields and paths, malformed JSON and
non-200 responses. Add another named check to that helper and its focused test if the join request contract changes.
