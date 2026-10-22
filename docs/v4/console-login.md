# Console login

People sign in to Command Center with an email and a password. The first role is **Super Administrator**
(`super_admin`, every permission). Roles live in one mapping, `ROLE_PERMISSIONS` in `control/console_auth.py`; a
new role is added there without touching call sites, which ask for permission identifiers such as
`integrations.manage`, `projects.manage` or `nodes.manage`. A Super Administrator passes every owner-only and
role-aware check.

## Installation turns it on

`aiflow4 install control --version vX.Y.Z --domain D --admin-email you@example.com` (see
[control-install.md](control-install.md)):

- sets `AIFLOW4_CONSOLE_LOGIN=1` and the external `https://<domain>` `AIFLOW4_PUBLIC_URL` in
  `/etc/aiflow/secrets/control/aiflow4.env` (an owner's explicit login setting `0` is kept); the signing key
  `AIFLOW4_CONSOLE_SESSION_SECRET` is generated there too;
- in its `admin` step runs `aiflow4 console-user seed --role super_admin` from the release image, after the
  migrations and before the deploy. The generated password is written once to
  `/etc/aiflow/secrets/console/super-admin-password` (0600) and is never printed;
- remembers the email in `/etc/aiflow/control.env` (`AIFLOW4_CONSOLE_ADMIN_EMAIL`), so later runs and upgrades need
  no `--admin-email` and change nothing for an existing user.

After the install the owner opens `https://<domain>/login` and only uses the UI. The lab's Super Administrator is
`jooservices@gmail.com`. The environment variable and the CLI below remain installer and developer tools. The
installer sets `AIFLOW4_PUBLIC_URL` to the public `https://` address so the cookie is marked `Secure`. Without
`AIFLOW4_CONSOLE_LOGIN=1` the console is open as before.

## What the gate does

- Console pages (`/cc`, `/delivery`, `/business`, ...) without a session redirect to `/login?next=...`.
- Every other path, including every console read API, answers 401 without a session.
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
| `GET /login` | the sign-in page |
| `POST /v1/auth/login` | `{email, password}`; returns the user and sets the HttpOnly `aiflow4_session` cookie (SameSite=Strict, Secure behind HTTPS, 12 hours) |
| `POST /v1/auth/logout`, `GET /logout` | clears the cookie |
| `GET /v1/auth/me` | the signed-in user, or 401 |

Because the cookie is Strict, a console page reached from another site arrives without it; the sign-in page then
asks `/v1/auth/me` (a same-site request that carries the cookie) and continues to `next` without a new sign-in.

## Sign-in protection

- **Rate limits first.** Before any database lookup Redis counts attempts per client IP (30) and per IP+email (10)
  in a 15-minute window; above that the answer is 429 with `Retry-After`. Forwarding headers are used only when the
  socket peer matches a CIDR in `AIFLOW4_TRUSTED_PROXIES`; otherwise the socket peer address is used, even if a
  direct-origin caller supplies `CF-Connecting-IP` or `X-Forwarded-For`. Configure only the proxy CIDRs that
  sanitize these headers, and restrict origin ingress at the host/network firewall so only those proxies can reach
  the published HTTP port.
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
`console_users` (migration `009_console_users.sql`).

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
