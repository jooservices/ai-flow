# Security installation structure (JAIF-533)

`install_security(app, ctx)` in `aiflow/v4/control/security.py` is a short function that runs the legacy-secret migrations and installs the security parts in a fixed order. Middleware order and route order are observable, so `tests/v4/test_security_structure_parity.py` pins both.

| Part | Module | Owns |
|---|---|---|
| Shared helpers | `security_guard.py` | `SecurityKit` (`session`, `require`, `ip_of`), CSRF/CSP constants |
| Middleware | `security_middleware.py` | `audit_changes`, `csrf`, `limits_and_headers` (registered in this order; the last is outermost) |
| Settings | `security_settings.py` | session length and the two-factor requirement |
| Sessions and people | `security_people.py` | active sessions, forced sign-out, people list, two-factor reset, unlock, the read-only audit log |
| Two-factor | `security_mfa.py` | status, setup, enable, recovery codes, disable |

To add a security route, put it in the part that owns it and install it from `install_security` in the position the security screen lists it.

## Routes

The routes are unchanged by the split: `GET /v1/security/settings`, `PUT /v1/security/settings`, `GET /v1/security/sessions`, `POST /v1/security/sessions/{sid}/revoke`, `POST /v1/security/users/{email}/sign-out`, `GET /v1/security/users`, `POST /v1/security/users/{email}/mfa/reset`, `POST /v1/security/users/{email}/unlock`, `GET /v1/security/mfa`, `POST /v1/security/mfa/setup`, `POST /v1/security/mfa/enable`, `POST /v1/security/mfa/recovery-codes`, `POST /v1/security/mfa/disable` and `GET /v1/security/audit`.
