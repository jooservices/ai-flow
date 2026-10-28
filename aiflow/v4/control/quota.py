"""Read the run and probe history needed for account quota forecasts."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..core.quota import account_awareness


def assessments(db, accounts: list[dict] | None = None, now: datetime | None = None) -> dict[str, dict]:
    now = now or datetime.now(timezone.utc)
    accounts = accounts or db.all("SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id")
    if not accounts:
        return {}
    ids = [str(a["id"]) for a in accounts]
    runs_by_account: dict[str, list[dict]] = defaultdict(list)
    for row in db.all("SELECT account_id, ticket_key, provider, model, outcome, reset_at, started_at, ended_at,"
                      " extract(epoch FROM ended_at-started_at) AS duration_s, tokens_in, tokens_out, cost_usd"
                      " FROM runs WHERE account_id=ANY(%s) AND started_at >= %s AND started_at <= %s"
                      " AND status<>'running' ORDER BY started_at", (ids, now - timedelta(days=35), now)):
        runs_by_account[str(row["account_id"])].append(row)
    samples_by_account: dict[str, list[dict]] = defaultdict(list)
    for row in db.all("SELECT account_id, sampled_at, observed_at, source, quota FROM account_quota_samples"
                      " WHERE account_id=ANY(%s) AND sampled_at >= %s AND sampled_at <= %s ORDER BY sampled_at",
                      (ids, now - timedelta(days=45), now)):
        samples_by_account[str(row["account_id"])].append(row)
    return {str(a["id"]): account_awareness(str(a.get("provider") or ""),
                                            (a.get("quota") or {}) if a.get("quota_source") else {},
                                            runs_by_account[str(a["id"])], samples_by_account[str(a["id"])], now)
            for a in accounts}


def account_assessment(db, account_id: str, provider: str, quota: dict, now: datetime | None = None) -> dict:
    metadata = db.one("SELECT quota_source FROM accounts WHERE id=%s", (account_id,)) or {}
    return assessments(db, [{"id": account_id, "provider": provider, "quota": quota,
                             "quota_source": metadata.get("quota_source", "")}], now).get(account_id, {})


def account_assessment_in(connection, account_id: str, provider: str, quota: dict,
                          now: datetime | None = None) -> dict:
    """Build a forecast from the caller's transaction so a new observation and forecast commit together."""
    now = now or connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    runs = connection.execute(
        "SELECT ticket_key,provider,model,outcome,reset_at,started_at,ended_at,"
        "extract(epoch FROM ended_at-started_at) AS duration_s,tokens_in,tokens_out,cost_usd"
        " FROM runs WHERE account_id=%s AND started_at >= %s AND started_at <= %s AND status<>'running'"
        " ORDER BY started_at", (account_id, now - timedelta(days=35), now)).fetchall()
    samples = connection.execute(
        "SELECT sampled_at,observed_at,source,quota FROM account_quota_samples"
        " WHERE account_id=%s AND sampled_at >= %s AND sampled_at <= %s ORDER BY sampled_at",
        (account_id, now - timedelta(days=45), now)).fetchall()
    return account_awareness(provider, quota or {}, runs, samples, now)
