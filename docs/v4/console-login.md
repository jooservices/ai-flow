# Console login

People sign in to Command Center with an email and a password. Their assigned roles map to permissions in
`ROLE_PERMISSIONS` in `control/console_auth.py`; multiple roles combine their grants. The session endpoint returns
the effective permission list, and the UI builds its menu from that list. The API independently checks the same
table for direct view URLs and protected actions. See [Role-based console views](role-based-views.md) for the role
table, landing page priority, people management and view-as behavior.

## First start

The control installer turns on `AIFLOW4_CONSOLE_LOGIN=1` and generates the session signing key. It does not need an
Administrator email or password. With no console user, `/login` serves the first-run setup screen instead of the
sign-in form. The browser asks for the public address, installation ownership capability, email and password, then
creates the first Administrator and signs them in. The capability is checked against the installation's
`AIFLOW4_SETUP_OWNERSHIP_CAPABILITY` and atomically consumed with first-user creation; the public status endpoint
never returns it. On an interactive first install, choose the capability during the install command's hidden
prompt and enter the same value in the browser.
Passwords are stored only as salted scrypt hashes; the setup request and audit record do not retain the password.
The selected public address is kept in PostgreSQL and used by every API replica for provider callbacks and generated
links, so no environment file edit is needed.

After the first Administrator exists, `/setup` redirects to the Dashboard. Its "Finish setting up AI Flow" panel
checks Jira, GitHub, a machine, AI accounts, the first project and a sample ticket. Each incomplete step names the
next action in plain words. Jira and GitHub actions stay in Integrations, machine join and account sign-in stay in
Machines and AI, and project setup stays in Projects. A skipped step remains on the Dashboard with a resume link.
The panel disappears only after every step is ready. The user-created password is never written to a local file.

An existing Administrator is still supported: a prior install can keep using its account and sign in at `/login`.
The optional installer `--admin-email` and `aiflow4 console-user seed` command remain available for controlled legacy
and CI setups, not as a required fresh-install step. The installer sets the initial public URL from `--domain` so
the console can be reached; the setup screen lets the Administrator confirm or change the browser-visible address.
HTTPS is recommended so the session cookie is marked `Secure`. Without `AIFLOW4_CONSOLE_LOGIN=1`, the console is open
as before.

## What the gate does

- Console pages (`/cc`, `/board`, `/delivery`, `/business`, ...) without a session redirect to `/login?next=...`.
- A signed-in person who lacks a view permission receives a clear 403 page; a forbidden API call receives 403 and is recorded.
- Unauthenticated API paths answer 401 without a session.
- A request with an `Authorization` header passes only to routes that verify a bearer token themselves: a
  dependency or handler with an `authorization` header parameter (nodes, workers, reviewer, owner, release files,
  `POST /v1/policy/decide`) or a handler marked `@checks_bearer` (the owner-token Jira routes). The route still
  refuses a wrong token; adding the header to a console API call does not help.
- Always open: `/healthz`, `/login`, `/logout`, `/v1/auth/*`, the node join and install endpoints, and the Jira and
  GitHub consent callbacks (they come back from another site without the Strict cookie and are checked by their
  one-use state).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /login` | first-run setup when no console user exists, otherwise the sign-in page |
| `GET /setup` | first Administrator bootstrap before any user exists; redirects to the Dashboard afterwards |
| `GET /v1/setup/status` | checks each first-run step and reports its missing action |
| `POST /v1/setup/bootstrap` | creates the first Administrator and saves the public address after the installation capability is supplied and consumed |
| `POST /v1/setup/steps/{step}` | skip a step or resume it later |
| `POST /v1/setup/sample-ticket` | creates and confirms the sample ticket in Intake; the Dashboard panel remains until all steps are ready |
| `POST /v1/auth/login` | `{email, password}`; returns the user and sets the HttpOnly `aiflow4_session` cookie (SameSite=Strict, Secure behind HTTPS, 12 hours) |
| `POST /v1/auth/logout`, `GET /logout` | clears the cookie |
| `GET /v1/auth/me` | the signed-in user, or 401 |
| `GET /v1/console/session` | the signed-in user, assigned roles, effective permissions, role options and sign-in landing page |

Because the cookie is Strict, a console page reached from another site arrives without it; the sign-in page then
asks `/v1/auth/me` (a same-site request that carries the cookie) and continues to the user's landing page without a new sign-in.

## Sign-in protection

- **Rate limits first.** Before any database lookup Redis counts attempts per client IP (30) and per IP+email (10)
  in a 15-minute window; above that the answer is 429 with `Retry-After`. Forwarding headers are used only when the
  socket peer matches a CIDR in `AIFLOW4_TRUSTED_PROXIES`; otherwise the socket peer address is used, even if a
  direct-origin caller supplies `CF-Connecting-IP` or `X-Forwarded-For`. Installation discovers the private CIDR
  assigned to the local Traefik edge network and writes its canonical value to `AIFLOW4_TRUSTED_PROXIES`, so
  forwarded client addresses do not share one rate-limit bucket. Restrict origin ingress at the host/network
  firewall so only the published proxy path can reach the HTTP port.
- **Same answer.** Unknown email, wrong password, locked account and disabled account all answer
  `401 {"detail": "invalid email or password"}` after the same scrypt work (a dummy hash for unknown, locked and
  disabled users).
- **Lockout.** Five wrong passwords in a row lock the account for 15 minutes. When the lock expires,
  `failed_attempts` is reset to 0. A successful sign-in or `reset-password` also clears it.
- **Off the event loop.** The whole attempt (Redis, database, scrypt) runs in a worker thread, so a sign-in never
  blocks other requests.
- **Bounded audit.** Successful sign-ins are written to `write_audit` (target `console`). Failed ones are written at
  most 30 times a minute across the service, so a flood cannot cause unbounded writes. No password is ever written.

## Users

Passwords are stored only as salted scrypt hashes (`hashlib.scrypt`, no new dependency) in the table
`console_users` (migrations `009_console_users.sql` and `011_console_role_views.sql`). Administrators add and
manage people from the **Users and roles** view. Adding a person shows a generated one-time password once; role,
project and account changes are audited. The last active Administrator cannot lose the Administrator role or be
disabled.

```sh
aiflow4 console-user seed --email you@example.com --role super_admin --password-file /root/you.pw
aiflow4 console-user reset-password --email you@example.com --password-file /root/you.pw
aiflow4 console-user list
```

`seed` creates the user once with a generated 32-character password; running it again changes nothing (the file is
not touched either). The password file is always private: it is opened with `O_NOFOLLOW` (a symlink at the path is
refused), an existing file must be a regular file of the same user with one link, and mode 0600 is set on the open
descriptor. The file is written and synced inside the database transaction, before the commit: when the write
fails, the new user (or the new password of `reset-password`) is rolled back, so an undelivered password never
replaces a working one. The password is never printed.

Tests: `tests/v4/test_console_login.py`, and the `admin` step in `tests/v4/test_control_install.py`.
