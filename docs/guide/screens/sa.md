# SA health

**Assumes:** you are signed in as an Owner or Super Administrator, the roles with `console.write`, and nodes have sent at least one heartbeat. Thresholds come
from `aiflow-control/sa.yaml`; without it the defaults apply (see `docs/v4/sa-health.md` in the repository).

SA health (`/sa`) is read-only. It never changes the fleet.

## What you see

- **Capacity recommendations**: add a node when slots stay above the configured percentage for the configured time; add an account for a role whose accounts all stay in quota cooldown; review a node or account that has been idle for the configured number of days.
- **Nodes and capacity**: heartbeat health, system load and slot use.
- **Account access, quota and credentials**: signed-out accounts, failed checks and credential expiry (shown as `unknown` when the provider does not report it).
- **AI blocked by policy** and **by provider**: denied commands and accounts in cooldown or signed out.
- **Network errors and retries**, with a link to OpenObserve for log detail.
- **Incidents**: the open ones, each with a [runbook](../runbooks/index.md).
