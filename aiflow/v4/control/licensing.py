"""Licence activation, plan limits, grace periods and project usage reports.

Licence files are compact EdDSA JWTs signed by the licence issuer. The token carries only the plan, expiry and
installation identifier. Online activation sends only the entered key and this installation identifier; the
issuer returns the same signed token used by offline activation.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from .db import J

GRACE_PERIOD = timedelta(days=14)
PLAN_DEFAULTS: dict[str, dict[str, int]] = {
    "free": {"machines": 1, "ai_accounts": 2, "projects": 3, "users": 3},
    "team": {"machines": 5, "ai_accounts": 12, "projects": 25, "users": 25},
    "business": {"machines": 50, "ai_accounts": 100, "projects": 250, "users": 250},
}
LIMIT_LABELS = {"machines": "Machine", "ai_accounts": "AI account", "projects": "Project", "users": "User"}


class LicenseError(ValueError):
    """An activation or plan-limit operation was refused with a safe message for the console."""


def initialize(db) -> None:
    """Create the single installation record on first use without storing a licence key."""
    db.run("INSERT INTO licensing_state (id, installation_id) VALUES (1,%s) ON CONFLICT (id) DO NOTHING",
           (str(uuid.uuid4()),))


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _row(db) -> dict[str, Any]:
    initialize(db)
    row = db.one("SELECT * FROM licensing_state WHERE id=1")
    if not row:  # defensive against a concurrent schema restore
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
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if activation and str(exc) == "licence has expired":
            raise LicenseError("This licence has expired. Use a current licence key or file.") from None
        if activation:
            raise LicenseError("Licence rejected: the signature, plan, expiry, or installation ID is invalid.") from None
        raise


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


def snapshot(db, public_key: str, now: datetime | None = None) -> dict[str, Any]:
    """Return licence status and record the first time a stored signed licence is found invalid."""
    now = _now(now)
    row = _row(db)
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
        except (jwt.InvalidTokenError, KeyError, TypeError, ValueError, OverflowError):
            reason = "invalid"
            if not invalid_since:
                invalid_since = now
                db.run("UPDATE licensing_state SET invalid_since=%s, updated_at=now() WHERE id=1 AND invalid_since IS NULL",
                       (invalid_since,))
                invalid_since = db.one("SELECT invalid_since FROM licensing_state WHERE id=1")["invalid_since"]
            plan = plan if plan in PLAN_DEFAULTS else "free"

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
    }


def _iso(value: datetime | None) -> str | None:
    return _now(value).isoformat() if value else None


def save_limits(db, plan: str, limits: dict[str, Any], public_key: str) -> dict[str, Any]:
    plan = plan.lower().strip()
    if plan not in PLAN_DEFAULTS:
        raise LicenseError("Choose Free, Team or Business.")
    if set(limits) != set(PLAN_DEFAULTS[plan]):
        raise LicenseError("Provide machine, AI account, project and user limits.")
    cleaned = {key: _bounded_limit(limits[key]) for key in PLAN_DEFAULTS[plan]}
    row = _row(db)
    all_limits = _key_map(row.get("plan_limits"))
    all_limits[plan] = cleaned
    db.run("UPDATE licensing_state SET plan_limits=%s, updated_at=now() WHERE id=1", (J(all_limits),))
    return snapshot(db, public_key)


def resource_counts(ctx) -> dict[str, int]:
    return {
        "machines": int(ctx.db.one("SELECT count(*) AS count FROM nodes")["count"]),
        "ai_accounts": int(ctx.db.one("SELECT count(*) AS count FROM accounts")["count"]),
        "projects": len(ctx.repo().projects),
        "users": int(ctx.db.one("SELECT count(*) AS count FROM console_users WHERE disabled=false")["count"]),
    }


def require_writable(db, public_key: str) -> dict[str, Any]:
    state = snapshot(db, public_key)
    if state["read_only"]:
        raise LicenseError("This installation is read-only because its licence is invalid or expired. Activate or renew a licence to continue.")
    return state


def require_capacity(ctx, resource: str, count: int | None = None, *, public_key: str) -> dict[str, Any]:
    if resource not in LIMIT_LABELS:
        raise LicenseError("Unknown licence limit.")
    current = resource_counts(ctx)[resource] if count is None else int(count)
    return require_count_capacity(ctx.db, resource, current, public_key=public_key)


def require_count_capacity(db, resource: str, current: int, *, public_key: str) -> dict[str, Any]:
    """Check a known count, for resources whose creator has only a DB handle (console users)."""
    state = require_writable(db, public_key)
    if resource not in LIMIT_LABELS:
        raise LicenseError("Unknown licence limit.")
    limit = state["limits"][resource]
    if current >= limit:
        label = LIMIT_LABELS[resource]
        plural = label + "s"
        location = " in the Licence screen" if resource != "users" else " with the console-user command"
        raise LicenseError(f"{plural} limit of {limit} reached ({current}/{limit}). Remove an unused {label.lower()} or raise the limit{location}.")
    return state


def enforce_account_entitlement(ctx, account_id: str, *, public_key: str) -> None:
    """Keep excess configured accounts from starting new runs while leaving their running work alone."""
    state = require_writable(ctx.db, public_key)
    limit = state["limits"]["ai_accounts"]
    account_ids = [str(row["id"]) for row in ctx.db.all("SELECT id FROM accounts ORDER BY id")]
    if account_id in account_ids and account_ids.index(account_id) >= limit:
        raise LicenseError(f"AI accounts limit of {limit} reached ({len(account_ids)}/{limit}). Disable or remove extra AI accounts or raise the limit in the Licence screen.")


def usage_report(db) -> list[dict[str, Any]]:
    """Aggregate ticket completions and AI usage by project and UTC calendar month of run start."""
    rows = db.all(
        "SELECT project, to_char(date_trunc('month', started_at AT TIME ZONE 'UTC'), 'YYYY-MM') AS month,"
        " count(DISTINCT ticket_key) FILTER (WHERE status='finished' AND outcome='done') AS tickets_finished,"
        " coalesce(sum(tokens_in),0) AS tokens_in, coalesce(sum(tokens_out),0) AS tokens_out,"
        " coalesce(sum(cost_usd),0)::numeric(12,6) AS cost_usd"
        " FROM runs GROUP BY project, date_trunc('month', started_at AT TIME ZONE 'UTC')"
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
