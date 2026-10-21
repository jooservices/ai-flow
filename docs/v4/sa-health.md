# SA system health dashboard

The read-only SA dashboard is available at `/sa` beside `/cc` and `/delivery`. It reads node heartbeats, account
probes, runs, events and open incidents from the v4 registry. The page links to OpenObserve for log detail; it
does not call the OpenObserve API.

## Recommendations

Thresholds live in `aiflow-control/sa.yaml` and are read with the control repo snapshot:

```yaml
thresholds:
  slots_used_percent: 80
  slots_duration_minutes: 30
  role_cooldown_minutes: 15
  idle_days: 7
  credential_expiry_days: 7
```

If `sa.yaml` or an individual key is absent, these defaults apply. The control plane starts a fleet slot-pressure
timer when fresh online heartbeats show at least the configured percentage in use. The dashboard recommends
adding a node after the pressure lasts for the configured duration. It recommends adding an account for a role
when every configured account in that role has remained in quota cooldown for the configured cooldown period.
It recommends reviewing a node or account when its recorded idle period reaches `idle_days`. These are
recommendations only; the dashboard does not change the fleet.

Node idle time starts with the first zero-slot heartbeat. Account idle time starts when a probe, run completion or
worker heartbeat reports the account idle. The registry cannot infer idle time from before it first observes that
state.

## Credentials and account checks

Structured CLI events can report `credential_expires_at` or `credentialExpiresAt`, or `expires_at` / `expiresAt`
inside a `credential`, `credentials` or `auth` object. The worker probe stores that timestamp and the node agent
forwards it in heartbeats. Missing or unparseable expiry information is shown as `unknown`. The dashboard warns
when a known expiry is within the configured warning window. Signed-out accounts and failed probes appear in the
account table as soon as the control API records them.

## Blocked AI and network errors

Policy denials show the account, ticket and denied command. Provider blocks list accounts in quota cooldown or
signed-out state. Network diagnostics combine registry events (including a node heartbeat error buffered by the
node agent until control connectivity returns) and runs whose `error_kind` is `network`. Retry indicators come
from the recorded event or run retry metadata when present.

The OpenObserve link opens the existing `/o2/` route in a new tab. The console reads the SA data from `/v1/state`;
no browser request is made to OpenObserve.
