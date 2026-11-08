# Authorize an AI account from the console

Owners authorize a provider account on its machine from **Machines and AI**, without a shell. The machine runs the
provider CLI's own login inside the account's private home. The console shows the provider link and code; credentials
stay on the machine.

## Steps

1. **Start** — `POST /v1/machines/{name}/accounts` or `POST /v1/accounts/{account_id}/sign-in` queues a sign-in request.
2. **Relay** — the node's next heartbeat hands the request to its agent, which starts the CLI in a PTY and posts the
   provider link and device code to `POST /v1/accounts/{account_id}/sign-in/status`.
3. **Authorize** — the owner opens the link in a browser that is already signed in to the provider and approves it.
4. **Check** — the node's first successful credential probe completes the sign-in; the owner then chooses
   Finish sign-in.

## Link lifetime

A provider link is valid for 15 minutes (`SIGN_IN_LINK_LIFETIME` in `aiflow/v4/control/features/fleet/routes_accounts.py`).
The window starts when the node reports a new link. Repeating the same link does not extend it.

- While the window is open, the owner's read of the link returns it with its `expires_at`.
- After it passes, the read is refused and a waiting report for that request is refused. Failed and completed reports
  are still accepted. The owner starts a new sign-in, which replaces the request id, so the old request id is refused too.

## Routes

- `GET /v1/accounts/{account_id}/sign-in` returns the live provider link and code for an owner, or 410 once the link has expired.
- `POST /v1/accounts/{account_id}/sign-in/status` lets the assigned node report its handoff; it refuses a waiting report after the link expired.

## Adding a step

Give each step a module-level function with explicit inputs, and keep the window in one constant. Cover the rule with
a test in `tests/v4/test_ai_authorize_link_expiry.py`.

## Not yet covered

- Pasting a code back into the provider CLI (Claude Code) and an API key for Grok are not implemented.
- Finish sign-in is still an owner click; the device flow does not complete on its own.
- An expired link still leaves the node's CLI waiting until the provider or the next sign-in ends it.

A provider link is valid for 15 minutes from the moment the node first reports it. Reading an expired link (or a link stored before expiry existed) answers 410; a node's repeated `waiting` report of the same expired link answers 409, while a different link starts a new 15-minute window. The policy lives in the fleet `service`, the SQL in the fleet `repository`.
