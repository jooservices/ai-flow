# Console login

People sign in to Command Center with an email and a password. The first role is **Super Administrator**
(`super_admin`, every permission); more roles are added to `ROLE_PERMISSIONS` in `control/console_auth.py`
without touching call sites, which ask `role_satisfies(user.role, {"owner"})`.

## Turn it on

Set `AIFLOW4_CONSOLE_LOGIN=1` and a signing key of at least 32 bytes in `AIFLOW4_CONSOLE_SESSION_SECRET` for
control-api. Set `AIFLOW4_PUBLIC_URL` to the public `https://` address so the cookie is marked `Secure`.
Without the setting the console is open as before.

## What the gate does

- Console pages (`/cc`, `/delivery`, `/business`, ...) without a session redirect to `/login?next=...`.
- Every other path answers 401 without a session.
- A request with an `Authorization` header passes only on routes that verify a bearer token themselves (nodes,
  workers, reviewer, owner, release files). Adding the header to a console API call does not help.
- `/healthz`, `/login`, `/logout`, `/v1/auth/*` and the node join and install endpoints stay open.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /login` | the sign-in page |
| `POST /v1/auth/login` | `{email, password}`; sets the HttpOnly `aiflow4_session` cookie (SameSite=Lax, 12 hours) |
| `POST /v1/auth/logout`, `GET /logout` | clears the cookie |
| `GET /v1/auth/me` | the signed-in user, or 401 |

SameSite is Lax so the browser still sends the cookie on the redirect back from the Jira and GitHub consent pages.
Wrong email and wrong password give the same answer. Five wrong passwords lock the account for 15 minutes.
Every attempt is written to `write_audit` (target `console`) without any password.

## Users

Passwords are stored as salted scrypt hashes (standard library). Users live in the table `console_users`.

```sh
aiflow4 console-user seed --email you@example.com --role super_admin --password-file /root/you.pw
aiflow4 console-user reset-password --email you@example.com --password-file /root/you.pw
aiflow4 console-user list
```

`seed` creates the user once with a generated 32-character password written to the file (mode 0600); the password
is never printed, and running it again changes nothing. `reset-password` writes a new one and clears a lock.

Tests: `tests/v4/test_console_login.py`.
