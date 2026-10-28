"""Licence activation, plan limits, grace periods and project usage reports.

Licence files are compact EdDSA JWTs signed by the licence issuer. The token carries only the plan, expiry and
installation identifier. Online activation sends only the entered key and this installation identifier; the
issuer returns the same signed token used by offline activation.
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from .db import J

log = logging.getLogger("aiflow4.licensing")

GRACE_PERIOD = timedelta(days=14)
LIMIT_INCIDENT_KIND = "licence_limit"
# Shared by resource admission and limit updates so they observe one another atomically.
RESOURCE_ADMISSION_LOCK = (1095320132, 233)
PLAN_DEFAULTS: dict[str, dict[str, int]] = {
    "free": {"machines": 3, "ai_accounts": 6, "projects": 3, "users": 3},
    "team": {"machines": 5, "ai_accounts": 12, "projects": 25, "users": 25},
    "business": {"machines": 50, "ai_accounts": 100, "projects": 250, "users": 250},
}
LIMIT_LABELS = {"machines": "Machine", "ai_accounts": "AI account", "projects": "Project", "users": "User"}


class LicenseError(ValueError):
    """An activation or plan-limit operation was refused with a safe message for the console."""


def lock_resource_admission(connection) -> None:
    """Serialize a resource admission with other admissions and changes to plan limits."""
    connection.execute("SELECT pg_advisory_xact_lock(%s,%s)", RESOURCE_ADMISSION_LOCK)


def initialize(db) -> None:
    """Create the single installation record on first use without storing a licence key."""
    db.run("INSERT INTO licensing_state (id, installation_id) VALUES (1,%s) ON CONFLICT (id) DO NOTHING",
           (str(uuid.uuid4()),))


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _row(db) -> dict[str, Any]:
    row = db.one("SELECT * FROM licensing_state WHERE id=1")
    if not row:  # initialize only on first use; steady-state worker polls must not issue a no-op INSERT
        initialize(db)
        row = db.one("SELECT * FROM licensing_state WHERE id=1")
    if not row:  # defensive against a concurrent schema restore
        raise LicenseError("Licence state is unavailable. Please retry.")
    return row


def _row_in_connection(connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM licensing_state WHERE id=1").fetchone()
    if not row:
        connection.execute("INSERT INTO licensing_state (id, installation_id) VALUES (1,%s)"
                           " ON CONFLICT (id) DO NOTHING", (str(uuid.uuid4()),))
        row = connection.execute("SELECT * FROM licensing_state WHERE id=1").fetchone()
    if not row:
        raise LicenseError("Licence state is unavailable. Please retry.")
    return row


def _key_map(value: Any) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for plan, defaults in PLAN_DEFAULTS.items():
        raw = value.get(plan) if isinstance(value, dict) else None
        row = raw if isinstance(raw, dict) else {}
        result[plan] = {key: _bounded_limit(row.get(key, default)) for key, default in defaults.items()}
    return result


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1_000_000:
        raise LicenseError("Each limit must be a whole number between 1 and 1,000,000.")
    return value


def _decode_license(token: str, public_key: str, installation_id: str, now: datetime,
                    *, activation: bool = False) -> dict[str, Any]:
    if not public_key.strip():
        raise LicenseError("Licence verification is not configured. Set AIFLOW4_LICENSE_PUBLIC_KEY.")
    try:
        claims = jwt.decode(token, public_key.replace("\\n", "\n"), algorithms=["EdDSA"],
                            options={"verify_exp": False, "verify_iat": False, "verify_aud": False,
                                     "require": ["exp", "plan", "installation_id"]})
        expiry = int(claims["exp"])
        plan = str(claims["plan"]).lower()
        if plan not in PLAN_DEFAULTS or claims["installation_id"] != installation_id:
            raise ValueError("licence claims do not match this installation")
        if activation and datetime.fromtimestamp(expiry, timezone.utc) <= now:
            raise ValueError("licence has expired")
        return {"plan": plan, "expires_at": datetime.fromtimestamp(expiry, timezone.utc)}
    except jwt.InvalidKeyError:
        if activation:
            raise LicenseError("Licence verification is misconfigured. Check AIFLOW4_LICENSE_PUBLIC_KEY.") from None
        raise LicenseError("Stored licence cannot be verified with the configured public key.") from None
    except (jwt.PyJWTError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if activation and str(exc) == "licence has expired":
            raise LicenseError("This licence has expired. Use a current licence key or file.") from None
        if activation:
            raise LicenseError("Licence rejected: the signature, plan, expiry, or installation ID is invalid.") from None
        raise LicenseError("Stored licence cannot be verified with the configured public key.") from None


def activate(db, token: str, public_key: str, source: str, now: datetime | None = None) -> dict[str, Any]:
    """Validate completely before changing the current licence, so a refusal leaves state untouched."""
    now = _now(now)
    row = _row(db)
    claims = _decode_license(token.strip(), public_key, row["installation_id"], now, activation=True)
    db.run("UPDATE licensing_state SET plan=%s, expires_at=%s, license_token=%s, license_source=%s,"
           " invalid_since=NULL, updated_at=now() WHERE id=1",
           (claims["plan"], claims["expires_at"], token.strip(), source))
    return snapshot(db, public_key, now)


def activate_online(db, key: str, service_url: str, public_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Exchange a key with the configured issuer without sending installation or customer data."""
    key = key.strip()
    if not key:
        raise LicenseError("Enter a licence key.")
    if not public_key.strip():
        raise LicenseError("Licence verification is not configured. Set AIFLOW4_LICENSE_PUBLIC_KEY.")
    if not service_url.strip():
        raise LicenseError("Online licence activation is not configured. Use a signed licence file instead.")
    try:
        service = urlsplit(service_url.strip())
        valid_service = (service.scheme == "https" and service.hostname and not service.username
                         and not service.password and not service.query and not service.fragment)
    except ValueError:
        valid_service = False
    if not valid_service:
        raise LicenseError("Online licence activation requires a configured HTTPS service URL without credentials or fragment.")
    row = _row(db)
    try:
        response = httpx.post(service_url.strip(), json={"key": key, "installation_id": row["installation_id"]},
                              timeout=15, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("license_token") or "") if isinstance(payload, dict) else ""
    except (httpx.HTTPError, ValueError, TypeError):
        raise LicenseError("The licence service could not validate this key. Check the key and try again.") from None
    if not token:
        raise LicenseError("The licence service rejected this key or returned an invalid response.")
    return activate(db, token, public_key, "online", now)


def snapshot(db, public_key: str, now: datetime | None = None, *, connection=None) -> dict[str, Any]:
    """Return licence status and record the first time a stored signed licence is found invalid."""
    now = _now(now)
    row = _row(db) if connection is None else _row_in_connection(connection)
    limits_by_plan = _key_map(row.get("plan_limits"))
    token = str(row.get("license_token") or "")
    invalid_since = row.get("invalid_since")
    reason = ""
    status = "active"
    plan = "free" if not token else str(row.get("plan") or "free").lower()
    expiry = row.get("expires_at")

    if token:
        try:
            claims = _decode_license(token, public_key, str(row["installation_id"]), now)
            stored_expiry = _now(expiry) if expiry else None
            if claims["plan"] != plan or claims["expires_at"] != stored_expiry:
                raise ValueError("stored claims do not match")
            plan = claims["plan"]
            expiry = claims["expires_at"]
        except (LicenseError, jwt.InvalidTokenError, KeyError, TypeError, ValueError, OverflowError):
            reason = "invalid"
            if not invalid_since:
                if connection is None:
                    invalid_since = now
                    db.run("UPDATE licensing_state SET invalid_since=%s, updated_at=now() WHERE id=1 AND invalid_since IS NULL",
                           (invalid_since,))
                    invalid_since = db.one("SELECT invalid_since FROM licensing_state WHERE id=1")["invalid_since"]
                else:
                    recorded = connection.execute(
                        "UPDATE licensing_state SET invalid_since=%s, updated_at=now()"
                        " WHERE id=1 AND invalid_since IS NULL RETURNING invalid_since", (now,)).fetchone()
                    invalid_since = (recorded or connection.execute(
                        "SELECT invalid_since FROM licensing_state WHERE id=1").fetchone())["invalid_since"]
            plan = plan if plan in PLAN_DEFAULTS else "free"

    # No issuer key and no stored licence: nothing can ever be licensed, so nothing is restricted (self-hosted,
    # development, lab). A stored licence whose key went missing is still an invalid licence with its grace.
    unlicensed = not token and not str(public_key or "").strip()
    deadline = None
    if reason == "invalid":
        deadline = _now(invalid_since) + GRACE_PERIOD
        status = "grace" if now < deadline else "read_only"
    elif expiry and _now(expiry) <= now:
        reason = "expired"
        deadline = _now(expiry) + GRACE_PERIOD
        status = "grace" if now < deadline else "read_only"

    return {
        "installation_id": row["installation_id"], "status": status, "reason": reason,
        "plan": plan, "expires_at": _iso(expiry), "grace_until": _iso(deadline),
        "read_only": status == "read_only", "limits": limits_by_plan[plan], "plans": limits_by_plan,
        "source": row.get("license_source") or ("free" if not token else ""),
        "licence_required": not unlicensed, "limits_enforced": not unlicensed,
        "over_limit_since": _iso(row.get("over_limit_since")),
    }


def _iso(value: datetime | None) -> str | None:
    return _now(value).isoformat() if value else None


def status_fields(state: dict[str, Any]) -> dict[str, Any]:
    """Return the public status subset used by the console status endpoint."""
    fields = ("status", "reason", "plan", "expires_at", "grace_until", "read_only", "licence_required")
    return {key: state[key] for key in fields}


def save_limits(db, plan: str, limits: dict[str, Any], public_key: str) -> dict[str, Any]:
    plan = plan.lower().strip()
    if plan not in PLAN_DEFAULTS:
        raise LicenseError("Choose Free, Team or Business.")
    if set(limits) != set(PLAN_DEFAULTS[plan]):
        raise LicenseError("Provide machine, AI account, project and user limits.")
    cleaned = {key: _bounded_limit(limits[key]) for key in PLAN_DEFAULTS[plan]}
    with db.pool.connection() as connection:
        with connection.transaction():
            lock_resource_admission(connection)
            require_writable(db, public_key, connection=connection)
            row = connection.execute("SELECT plan_limits FROM licensing_state WHERE id=1 FOR UPDATE").fetchone()
            all_limits = _key_map((row or {}).get("plan_limits"))
            all_limits[plan] = cleaned
            connection.execute("UPDATE licensing_state SET plan_limits=%s, updated_at=now() WHERE id=1",
                               (J(all_limits),))
    return snapshot(db, public_key)


# Accounts removed from the console (or whose machine was removed) stay in the table but no longer count.
_ACTIVE_ACCOUNTS = ("SELECT id FROM accounts WHERE id NOT IN (SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL)"
                    " AND node NOT IN (SELECT name FROM nodes WHERE removed_at IS NOT NULL)")


def resource_counts(ctx, *, connection=None) -> dict[str, int]:
    query_one = (lambda sql, params=None: connection.execute(sql, params).fetchone()) if connection else ctx.db.one
    configured_account_ids = sorted({str(account.id) for account in ctx.repo().accounts})
    counts = query_one(
        "SELECT (SELECT count(*) FROM (" + _ACTIVE_ACCOUNTS +
        " UNION SELECT unnest(%s::text[])) AS active_accounts) AS ai_accounts,"
        " (SELECT count(*) FROM nodes WHERE removed_at IS NULL) AS machines,"
        " (SELECT count(*) FROM console_users WHERE disabled=false) AS users",
        (configured_account_ids,))
    return {
        "machines": int(counts["machines"]),
        "ai_accounts": int(counts["ai_accounts"]),
        "projects": len(ctx.repo().projects),
        "users": int(counts["users"]),
    }


def require_writable(db, public_key: str, *, connection=None) -> dict[str, Any]:
    state = snapshot(db, public_key, connection=connection)
    if state["read_only"]:
        raise LicenseError("This installation is read-only because its licence is invalid or expired. Activate or renew a licence to continue.")
    return state


def require_capacity(ctx, resource: str, count: int | None = None, *, public_key: str,
                     connection=None) -> dict[str, Any]:
    if resource not in LIMIT_LABELS:
        raise LicenseError("Unknown licence limit.")
    current = resource_counts(ctx, connection=connection)[resource] if count is None else int(count)
    return require_count_capacity(ctx.db, resource, current, public_key=public_key, connection=connection)


def require_count_capacity(db, resource: str, current: int, *, public_key: str, connection=None) -> dict[str, Any]:
    """Check a known count, for resources whose creator has only a DB handle (console users).

    Creating a new resource is refused at the limit even inside the over-limit grace period."""
    state = require_writable(db, public_key, connection=connection)
    if resource not in LIMIT_LABELS:
        raise LicenseError("Unknown licence limit.")
    if not state.get("limits_enforced", True):
        return state
    limit = state["limits"][resource]
    if current >= limit:
        label = LIMIT_LABELS[resource]
        if resource == "users":
            raise LicenseError(f"Users limit of {limit} reached ({current}/{limit}). Raise the user limit in the Licence screen before creating another account.")
        plural = label + "s"
        raise LicenseError(f"{plural} limit of {limit} reached ({current}/{limit}). Remove an unused {label.lower()} or raise the limit in the Licence screen.")
    return state


def over_limit_grace(ctx, state: dict[str, Any], now: datetime | None = None) -> bool:
    """True while usage above a newly applied or lowered limit is inside the grace period.

    The clock starts the first time usage is found above a limit and is cleared when usage is back within every
    limit. Machines, accounts and projects already over the limit keep working during it; only creating new
    ones is refused. The answer is cached on `state`. Limit incidents are closed here once nothing is stopped."""
    if "over_limit_grace" in state:
        return state["over_limit_grace"]
    now = _now(now)
    if not state.get("limits_enforced", True):
        state["over_limit_grace"] = False
        return False
    usage = resource_counts(ctx)
    # All entitlement checks for this request share the same fleet-size snapshot. A candidate
    # response can check many projects; recomputing these counts for every offered ticket scans
    # the same accounts and machines repeatedly.
    state["_resource_counts"] = usage
    over = sorted(key for key, limit in state["limits"].items() if usage.get(key, 0) > limit)
    since = state.get("over_limit_since")
    if over and not since:
        row = ctx.db.one("UPDATE licensing_state SET over_limit_since=%s, updated_at=now()"
                         " WHERE id=1 AND over_limit_since IS NULL RETURNING over_limit_since", (now,))
        since = _iso((row or ctx.db.one("SELECT over_limit_since FROM licensing_state WHERE id=1"))["over_limit_since"])
    elif not over and since:
        ctx.db.run("UPDATE licensing_state SET over_limit_since=NULL, updated_at=now() WHERE id=1")
        since = None
    deadline = _now(datetime.fromisoformat(since)) + GRACE_PERIOD if over and since else None
    in_grace = bool(deadline and now < deadline)
    state.update(over_limit_since=since, over_limit_until=_iso(deadline), over_limit_grace=in_grace)
    # Only close incidents that actually exist. Worker polls pass through this check twice (candidate and claim);
    # issuing four no-op UPDATEs for every request creates needless database work at fleet scale.
    open_resources = {str(row["subject"]) for row in ctx.db.all(
        "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL", (LIMIT_INCIDENT_KIND,))}
    for key in open_resources.intersection(LIMIT_LABELS):
        if in_grace or key not in over:
            _close_limit_incident(ctx, key)
    return in_grace


def _close_limit_incident(ctx, resource: str) -> None:
    try:
        from .incidents import close_incident
        close_incident(ctx, LIMIT_INCIDENT_KIND, resource,
                       "Work resumed: usage is within the limit, the limit was raised or the grace period applies.")
    except Exception as exc:  # noqa: BLE001 - closing an alert must never block work
        log.warning("licence limit incident could not be closed: %s", type(exc).__name__)


def _stop_work(ctx, resource: str, message: str, limit: int, usage: int) -> None:
    """A limit stops new work: open a critical incident naming it, then refuse with the same message."""
    label = LIMIT_LABELS[resource].lower()
    try:
        from .incidents import open_incident
        open_incident(ctx, LIMIT_INCIDENT_KIND, resource, "critical",
                      f"Licence limit stops new work: {label}s {usage}/{limit}",
                      f"{message} The grace period for usage above the limit has ended; new work is refused for "
                      f"{label}s beyond the first {limit}.")
    except Exception as exc:  # noqa: BLE001 - the refusal itself must still reach the worker
        log.warning("licence limit incident could not be opened: %s", type(exc).__name__)
    raise LicenseError(message)


def enforce_account_entitlement(ctx, account_id: str, *, public_key: str,
                                state: dict[str, Any] | None = None) -> None:
    """Keep excess configured accounts from starting new runs while leaving their running work alone."""
    state = state or require_writable(ctx.db, public_key)
    if not state.get("limits_enforced", True) or over_limit_grace(ctx, state):
        return
    limit = state["limits"]["ai_accounts"]
    usage = state.get("_resource_counts") or {}
    if int(usage.get("ai_accounts", limit + 1)) <= limit:
        return
    # A configured account can be intentionally absent from the database when startup sync skipped it at
    # capacity. Count the union so that this resource still cannot claim work through the node API.
    account_ids = sorted({str(row["id"]) for row in ctx.db.all(_ACTIVE_ACCOUNTS + " ORDER BY id")}
                         | {str(account.id) for account in ctx.repo().accounts})
    if account_id in account_ids and account_ids.index(account_id) >= limit:
        _stop_work(ctx, "ai_accounts",
                   f"AI accounts limit of {limit} reached ({len(account_ids)}/{limit}). Disable or remove extra AI accounts or raise the limit in the Licence screen.",
                   limit, len(account_ids))


def enforce_machine_entitlement(ctx, node: str, *, public_key: str,
                                state: dict[str, Any] | None = None) -> None:
    """Keep machines beyond the current plan from starting new work; active runs remain unaffected."""
    state = state or require_writable(ctx.db, public_key)
    if not state.get("limits_enforced", True) or over_limit_grace(ctx, state):
        return
    limit = state["limits"]["machines"]
    usage = state.get("_resource_counts") or {}
    if int(usage.get("machines", limit + 1)) <= limit:
        return
    node_names = [str(row["name"]) for row in ctx.db.all("SELECT name FROM nodes WHERE removed_at IS NULL ORDER BY name")]
    if node in node_names and node_names.index(node) >= limit:
        _stop_work(ctx, "machines",
                   f"Machines limit of {limit} reached ({len(node_names)}/{limit}). Disable or remove extra machines or raise the limit in the Licence screen.",
                   limit, len(node_names))


def enforce_project_entitlement(ctx, project: str, *, public_key: str,
                                state: dict[str, Any] | None = None) -> None:
    """Keep projects beyond the current plan from receiving new work."""
    state = state or require_writable(ctx.db, public_key)
    if not state.get("limits_enforced", True) or over_limit_grace(ctx, state):
        return
    limit = state["limits"]["projects"]
    usage = state.get("_resource_counts") or {}
    if int(usage.get("projects", limit + 1)) <= limit:
        return
    project_keys = sorted(ctx.repo().projects)
    if project in project_keys and project_keys.index(project) >= limit:
        _stop_work(ctx, "projects",
                   f"Projects limit of {limit} reached ({len(project_keys)}/{limit}). Disable or remove extra projects or raise the limit in the Licence screen.",
                   limit, len(project_keys))


def enforce_work_entitlement(ctx, account_id: str, node: str, *, public_key: str) -> dict[str, Any]:
    """Return the active plan state after checking the machine and account that would start new work."""
    state = require_writable(ctx.db, public_key)
    over_limit_grace(ctx, state)
    enforce_account_entitlement(ctx, account_id, public_key=public_key, state=state)
    enforce_machine_entitlement(ctx, node, public_key=public_key, state=state)
    return state


def usage_report(db) -> list[dict[str, Any]]:
    """Count tickets by their final Done time and report AI usage by run start month."""
    rows = db.all(
        "WITH run_usage AS ("
        " SELECT project, date_trunc('month', started_at AT TIME ZONE 'UTC') AS month_at,"
        " coalesce(sum(tokens_in),0) AS tokens_in, coalesce(sum(tokens_out),0) AS tokens_out,"
        " coalesce(sum(cost_usd),0)::numeric(12,6) AS cost_usd"
        " FROM runs GROUP BY project, date_trunc('month', started_at AT TIME ZONE 'UTC')"
        "), finished AS ("
        " SELECT project, date_trunc('month', done_at AT TIME ZONE 'UTC') AS month_at,"
        " count(*)::int AS tickets_finished FROM tickets"
        " WHERE status='Done' AND done_at IS NOT NULL"
        " GROUP BY project, date_trunc('month', done_at AT TIME ZONE 'UTC')"
        ") SELECT coalesce(r.project,f.project) AS project,"
        " to_char(coalesce(r.month_at,f.month_at),'YYYY-MM') AS month,"
        " coalesce(f.tickets_finished,0)::int AS tickets_finished,"
        " coalesce(r.tokens_in,0) AS tokens_in, coalesce(r.tokens_out,0) AS tokens_out,"
        " coalesce(r.cost_usd,0)::numeric(12,6) AS cost_usd"
        " FROM run_usage r FULL OUTER JOIN finished f USING (project,month_at)"
        " ORDER BY month DESC, project")
    return [{"project": row["project"], "month": row["month"],
             "tickets_finished": int(row["tickets_finished"]), "tokens_in": int(row["tokens_in"]),
             "tokens_out": int(row["tokens_out"]), "cost_usd": float(row["cost_usd"])} for row in rows]


def usage_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Project", "Month (UTC)", "Tickets finished", "Input tokens", "Output tokens", "Cost (USD)"])
    for row in rows:
        writer.writerow([row["project"], row["month"], row["tickets_finished"], row["tokens_in"],
                         row["tokens_out"], f"{row['cost_usd']:.6f}"])
    return output.getvalue()
