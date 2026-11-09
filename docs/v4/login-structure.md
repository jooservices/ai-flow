# Login installer structure

`create_app` installs the `console_login` feature (its sign-in, session and user routes) and then calls `install_login` in
`aiflow/v4/control/login.py`, the wiring point of the rest. Its calls keep the observable registration sequence: session
revocation, discovered routers, the optional login gate, then security middleware.

Sign-in, sessions and user management are the feature `aiflow/v4/control/features/console_login/` (JAIF-496), moved out of
`login_signin.py`, `login_sessions.py` and `login_users.py` with their behaviour unchanged:

- `routes.py` holds the routes, the request parsing and the 401/403 check of the user routes; `install(app, ctx)` registers
  them in the old order (login page, sign-in, sign-out, logout page, current user, user list, user creation).
- `service.py` holds the use cases without FastAPI: credential attempts, rate limits, the failed sign-in audit,
  session end and revocation, the current user and user creation. It returns a status and a body; the route builds the response.
- `repository.py` holds all SQL on `console_users`, `console_sessions` and the sign-in `write_audit` row.
- `session_cookies.py` holds the session and CSRF cookies and the session length limits.

`login_gate.py` still owns bearer-route detection, API permissions, project scoping, access refusals and redirects.
`login.py` retains the shared password, user and settings helpers used by existing callers.

To extend sign-in, add the SQL to `repository.py`, the decision to `service.py` and, when it is a new route, a handler to
`install` in `routes.py` at the position its order requires; keep `ctx` and settings explicit and keep the gate before
the security middleware.

## Routes

The routes are unchanged by the move; the documentation check ties a changed route module to a document that names them.

- `GET /login` serves the sign-in form, or the first-run setup wizard while no console user exists.
- `POST /v1/auth/login` checks the email, password and optional second factor, applies the rate limits and the lock, and on success opens a session with the signed and CSRF cookies.
- `POST /v1/auth/logout` ends the session named by the cookie, audits it and clears both cookies.
- `POST /logout` does the same and redirects the browser to `/login`.
- `GET /v1/auth/me` returns the signed-in user, roles, landing page and whether two-factor setup is still required, or 401.
- `GET /v1/auth/users` lists the console users with the active count and the licence limit, for a session that may manage users.
- `POST /v1/auth/users` creates a console user with a generated initial password, for a session that may manage users.
