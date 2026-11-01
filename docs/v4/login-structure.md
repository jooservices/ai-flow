# Login installer structure

`install_login` in `aiflow/v4/control/login.py` is the wiring point. Its calls keep the observable registration
sequence: sign-in routes, session routes, user management routes, discovered routers, the optional login gate, then
security middleware.

- `login_signin.py` owns the login page and endpoint, credential attempts, rate limits, and failed sign-in audit.
- `login_sessions.py` owns cookie creation and clearing, session revocation, logout, the current-user endpoint, and the
  shared user response shape.
- `login_users.py` owns the authenticated user list and account creation endpoints.
- `login_gate.py` owns bearer-route detection, API permissions, project scoping, access refusals, and redirects.

To extend login behavior, add the helper or route to the part that owns it and register it through that part's
`install_*` function. Keep `ctx` and settings explicit, keep route registration in the current order, and preserve the
position of the gate before security middleware. `login.py` retains the shared password, user, and settings helpers
used by existing callers. The routes remain `/login`, `/v1/auth/login`, `/v1/auth/logout`, `/logout`,
`/v1/auth/me`, and `GET`/`POST /v1/auth/users`.

## Routes in these modules

The routes are unchanged by the split; they are listed here because the documentation check ties a changed route module to a document that names its routes.

- `POST /v1/auth/login` (sign-in, `login_signin.py`)
- `POST /v1/auth/logout` (sessions, `login_sessions.py`)
- `GET /v1/auth/me` (sessions, `login_sessions.py`)
- `GET /v1/auth/users` and `POST /v1/auth/users` (user management, `login_users.py`)
