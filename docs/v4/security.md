# Security hardening (JAIF-231)

**Assumes:** you are signed in to the installed v4 release. Settings and the user list need the Administrator role;
everyone can manage their own two-factor sign-in.

AI Flow holds code access and AI credentials. This page lists what protects them, where to set it, and how it is tested
(`tests/v4/test_security.py`, `tests/v4/test_security_report.py`, `tests/v4/test_console_escaping.py`).
The coordinated disclosure policy is published in [`.github/SECURITY.md`](https://github.com/jooservices/ai-flow/blob/master/.github/SECURITY.md).

## Where people manage it: the Security screen

Console > **Security** (`/security`). Everybody who is signed in sees their own two-factor panel. People with the
`security.manage` permission (Administrator) also see session settings, signed-in sessions and people; the audit log
needs `security.audit` (also Administrator). All of it is also available as API under `/v1/security/*`.

### Security screen quick guide

- Set up two-factor sign-in with an authenticator app and receive single-use recovery codes. Store the codes safely;
  they are shown only when issued.
- Set how long a session lasts.
- See active sessions and sign one out. A forced sign-out ends that session and its next request is rejected.
- Read the audit log of changes to settings, people, roles and credentials. The audit log is read-only in the console.

## AC1 Two-factor sign-in, recovery codes, sessions

- **Two-factor sign-in (TOTP).** Set up on the Security screen: add the key to any authenticator app, enter a code, turn on.
  The sign-in page has a field for the code. A code works once.
- **Recovery codes.** Turning two-factor on shows ten single-use codes once (stored only as keyed hashes). A recovery code
  is typed in the same field as the authenticator code. "Issue new recovery codes" replaces the set.
- **Lost both?** An Administrator uses *Reset two-factor* on the people table; the person is signed out and sets it up
  again.
- **Require for administrators.** Two-factor sign-in is mandatory for the `administrator` role (the earlier `super_admin` and `owner` roles are now Administrator), regardless of
  the legacy `require_mfa` setting. A sign-in without two-factor is a limited session that can only open the Security
  screen to set it up. Administrators cannot turn it off; the Security screen displays this policy as always on.
- **Session length.** Setting *Session length* (5 minutes to 30 days, default 12 hours) applies to the next sign-in.
- **Forced sign-out.** *Sign out* on a session, or *Sign out everywhere* for a person, ends the session in the database.
  Every route that reads the session cookie checks it, so the next request on that session is refused (401). Logout,
  and a password reset, end sessions the same way.
- **Upgrade.** Cookies issued before this release carry no session id or version, so they bypass neither two-factor
  nor forced sign-out: they are refused and every person signs in once more.

## AC2 Secrets encrypted at rest

Secrets are encrypted with AES-256-GCM (`aiflow/v4/core/secretbox.py`). Secret writes fail until an external encryption
key is configured. The key is **not** in the database:

- `AIFLOW4_SECRET_KEY` (urlsafe base64 of 32 random bytes) or `AIFLOW4_SECRET_KEY_FILE` (a file, mode 0600, same format).
  Generate with `python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
- The Jira credential and OAuth app secret files, the GitHub App private key, and the authenticator secrets in
  `console_users`. Recovery codes are keyed hashes. Console passwords were already salted scrypt hashes. If
  `AIFLOW4_SECRET_KEY` is absent, authenticator secrets use a key derived from the console session secret (also outside the
  database). On first access with a key configured, existing plaintext Jira and GitHub secret files are upgraded in place;
  legacy plaintext authenticator secrets are migrated at startup. Notification channel credentials (email password, bot
  token, webhook URLs and tokens in `notification_channels.config`) require a valid external key: startup migrates legacy
  values transactionally and fails closed if the key is missing, invalid or cannot decrypt stored ciphertext. Delivery also
  refuses any plaintext notification credential. Keep the key with the other secrets and back it up separately: without
  it encrypted secrets cannot be read.

## AC3 Rate limits and sign-in guessing protection

- Every endpoint has a per-address budget (`AIFLOW4_RATE_LIMIT_PER_MIN`, default 600 a minute). Public endpoints (sign-in,
  install, join, provider callbacks, health) and anonymous changes have a tighter per-path budget
  (`AIFLOW4_RATE_LIMIT_PUBLIC_PER_MIN`, default 60). Over the budget the answer is `429` with `Retry-After`. `0` turns a
  budget off. Behind a proxy set `AIFLOW4_TRUSTED_PROXIES` so the real client address is used.
- Sign-in: budgets per address and per address+email (429), and 5 wrong passwords lock the account against
  **password-only** attempts for 15 minutes. After a sign-in budget is spent, the service still checks the credentials;
  only a valid password plus fresh authenticator or recovery code is allowed through. The lock never shuts the real owner
  out: someone who also gives a valid
  authenticator code or an unused recovery code is still checked and signed in, which clears the lock. A guesser does not
  have the second factor. An account without two-factor stays locked until the lock ends, or an Administrator uses
  *Unlock* on the Security screen. All refusals look the same.

## AC4 Cross-site requests and escaped text

- A state-changing request (`POST`, `PUT`, `PATCH`, `DELETE`) that uses the console session cookie must send the header
  `X-CSRF-Token`, an HMAC of that session; the console copies it from the `aiflow4_csrf` cookie. Without it, or with another
  session's token, the answer is `403`. Requests with an `Authorization` header (nodes, workers, owner token) are not
  browser-driven and are exempt. The pre-authentication sign-in request checks `Origin` or `Referer` against the service
  origin (and rejects `Sec-Fetch-Site: cross-site`). Logout uses `POST` and requires the CSRF token. The cookie is also
  `SameSite=Strict`.
- Console text from tickets, projects, nodes and integrations goes through `esc()` before it is put in a page; links are
  limited to `http(s)` and same-site paths. Pages carry a Content-Security-Policy (no foreign scripts, no framing).
  This escaping and link check was applied to the existing screens as well, and every browser request that changes
  something now carries the CSRF header.

**Console screens:** `cc`, `work`, `delivery`, `sa`, `lead`, `business`, `review`, `stakeholder`, `machines`, `users`, `knowledge`, `backup`, `integrations`, `projects`, `licensing`, `notifications`, `support`, `security`.

Their text and links are escaped as described above; their behaviour is otherwise unchanged.

## AC5 Scans on every pull request

Job `Security scan` in `.github/workflows/ci.yml` runs `deploy/v4/ci/security-scan.sh` (Trivy): dependencies (`uv.lock`),
the repository (secrets, misconfiguration) and the control-api container image. A finding at or above the blocking level
fails the job and blocks the merge when the check is required. The level is the repository variable
`SECURITY_SEVERITY_THRESHOLD` (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`; default `HIGH`), not a constant in the code.

There is no blanket suppression. The one exception is the container image scan, which uses `--ignore-unfixed`:
operating-system findings for which the Debian base has no fixed package cannot be acted on, and the image upgrades its
packages on every build. Dependency and repository findings, and every image finding that has a fix, still block.
`pyproject.toml` overrides fastembed's `pillow<13` cap so that pillow 13 (which fixes HIGH findings) is locked; fastembed
uses pillow only for image embeddings, which AI Flow does not use. The production
control image runs as the non-root user `aiflow` (`USER` in `deploy/v4/control/Dockerfile`, `user:` in the compose file).
Two narrow, reviewed exceptions exist: `deploy/v4/ci/trivy-secret.yaml` allows fake keys only in the named test fixture
files (any other file under `tests/` is scanned), and the repository scan skips `deploy/v4/ci/systemd.Dockerfile` and
`deploy/v4/ci/offline-target.Dockerfile`, throw-away CI test machines that need PID 1 as root and are never deployed.

## AC6 Release security report and disclosure policy

Every release runs the same scan on the release image and publishes `SECURITY-REPORT.md` with the release: the blocking
level, each check with counts per severity and PASS or FAIL, the test and install checks, and the disclosure policy link
(`aiflow/v4/core/security_report.py`). A failing scan stops the release before it is published. The coordinated disclosure
policy is [`.github/SECURITY.md`](https://github.com/jooservices/ai-flow/blob/master/.github/SECURITY.md): report privately through GitHub, acknowledgement in 3
working days, fixes in 30 or 90 days by severity.

## Routes that now write atomically with the audit log

These routes keep their request and response shapes. Their shared database helpers now run inside one transaction, so a
change and its audit entry are written together or not at all, and they carry the cross-site request token like every
other state-changing route:

- `POST /v1/decisions`, `POST /v1/decisions/{decision_id}/answer`, `POST /v1/decisions/{decision_id}/retire`
- `POST /v1/escalations/raise`, `POST /v1/escalations/triage`
- `GET /v1/github/ci-log`, `POST /v1/github/delete-branch`
- `POST /v1/integrations/github/connections/token`, `PUT /v1/integrations/github/connections/{connection_id}/token`,
  `POST /v1/integrations/github/connections/{connection_id}/test`, `DELETE /v1/integrations/github/connections/{connection_id}`
- `PUT /v1/projects/{key}/repositories`

## AC7 Audit log

Every change to settings, people, roles and credentials is appended to `audit_log` in the same database transaction as
the associated change, so an append failure rolls the change back. Entries include who, when, area, what and from
which address (never a secret value): security settings, people and role creation, password resets, two-factor changes,
forced sign-outs, sign-in and sign-out, owner-token Jira writes, OAuth callbacks, and signed-in state-changing requests
(by route, for example Jira and GitHub credential changes under `credentials`). The Security screen shows it read-only
with an area filter. There
is no route that writes, edits or deletes it, and database triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on the table.

## API routes

Sign-in, session and Security screen routes. Administrator-only ones are enforced on the server, not only hidden in
the console:

- `GET /v1/auth/me`
- `POST /v1/auth/login`
- `POST /v1/auth/logout`
- `POST /v1/auth/users`
- `GET /v1/security/audit`
- `GET /v1/security/mfa`
- `GET /v1/security/sessions`
- `GET /v1/security/settings`
- `GET /v1/security/users`
- `POST /v1/security/mfa/disable`
- `POST /v1/security/mfa/enable`
- `POST /v1/security/mfa/recovery-codes`
- `POST /v1/security/mfa/setup`
- `POST /v1/security/sessions/{sid}/revoke`
- `POST /v1/security/users/{email}/mfa/reset`
- `POST /v1/security/users/{email}/sign-out`
- `POST /v1/security/users/{email}/unlock`
- `PUT /v1/security/settings`

The control API's own audit record (`write_audit`) is written in the request's database transaction, so the record and
the change it describes commit or roll back together. The state-changing routes that use it:

- `DELETE /v1/accounts/{account_id}`
- `DELETE /v1/nodes/{name}`
- `GET /v1/analytics/usage`
- `PATCH /v1/console/users/{email}/disabled`
- `PUT /v1/console/users/{email}`
- `POST /v1/knowledge/items/{item_id}/{action}`
- `GET /v1/integrations/github/imports/{pending_id}/install`
- `GET /v1/integrations/github/installation-settings`
- `GET /v1/integrations/github/manifest/callback`
- `GET /v1/integrations/github/manifest/start`
- `GET /v1/integrations/github/setup`
- `GET /v1/reviews`
- `GET /v1/reviews/export.csv`
- `POST /v1/backups/key`
- `POST /v1/backups/run`
- `POST /v1/business/{key}/change-requests`
- `POST /v1/console/view-as`
- `POST /v1/console/view-as/stop`
- `POST /v1/github/comment`
- `POST /v1/github/pr`
- `POST /v1/github/token`
- `POST /v1/github/update-branch`
- `POST /v1/integrations/github/imports`
- `POST /v1/integrations/github/imports/{pending_id}/connect`
- `POST /v1/jira/{key}/comment`
- `POST /v1/jira/{key}/description`
- `POST /v1/jira/{key}/fields`
- `POST /v1/jira/{key}/transition`
- `POST /v1/knowledge/proposals/{proposal_id}/approve`
- `POST /v1/master/actions`
- `POST /v1/nodes/{name}/check-probes/token`
- `POST /v1/nodes/{name}/join-command`
- `POST /v1/nodes/{name}/operations`
- `POST /v1/notify/triage`
- `POST /v1/reviews/{key}/answer`
- `POST /v1/reviews/{key}/approve`
- `POST /v1/reviews/{key}/ask-ai`
- `POST /v1/reviews/{key}/reject`
- `POST /v1/reviews/{key}/rework`
- `POST /v1/reviews/{key}/skip-gate`
- `POST /v1/stakeholder/{key}/verdict`
- `PUT /v1/backups/config`
- `PUT /v1/master/autonomy/budget`
- `PUT /v1/nodes/{name}/work-acceptance`
- `PUT /v1/notifications/channels`
- `PUT /v1/role-health/settings`
