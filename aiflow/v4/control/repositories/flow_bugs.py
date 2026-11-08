"""Persistence for the durable repeated-flow-failure tracker and its normalized child rows."""
from __future__ import annotations

from collections import defaultdict


_MEMBER_FIELDS = {
    "ticket": "ticket",
    "step": "step",
    "provider": "provider",
    "account": "account",
}


def candidate_data(db) -> tuple[set[str], list[dict], list[dict]]:
    """Load recent terminal failures, tracked signatures and old ticket-stuck incidents for detection."""
    tracked = {row["signature"] for row in db.all("SELECT signature FROM flow_bug_tracker")}
    runs = db.all(
        "SELECT r.id, r.ticket_key, r.project, r.step, r.error_kind, r.outcome, r.summary, r.node, r.account_id,"
        " r.provider, r.started_at, r.tokens_in, r.tokens_out, r.cost_usd,"
        " coalesce(n.release_version, '') AS release_version FROM runs r"
        " LEFT JOIN nodes n ON n.name=r.node WHERE r.status<>'running' AND r.outcome IN ('failed','timeout')"
        " AND coalesce(r.ended_at,r.started_at) >= now() - interval '6 hours' ORDER BY r.started_at",
    )
    incidents = db.all(
        "SELECT subject,opened_at FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"
        " AND opened_at < now() - interval '30 minutes'",
    )
    return tracked, runs, incidents


def tracker(db, signature: str) -> dict | None:
    return db.one("SELECT * FROM flow_bug_tracker WHERE signature=%s", (signature,))


def open_trackers(db) -> list[dict]:
    return db.all("SELECT signature,ticket_key,bug_status,fixed_at,watch_until FROM flow_bug_tracker"
                  " WHERE bug_status='' OR lower(bug_status) NOT IN"
                  " ('done','closed','resolved','cancelled','canceled')")


def flow_bug_signatures(db) -> set[str]:
    return {row["signature"] for row in db.all("SELECT signature FROM flow_bug_tracker")}


def member_values(db, signature: str) -> dict[str, list[str]]:
    values = {kind: [] for kind in _MEMBER_FIELDS}
    for row in db.all("SELECT kind,value FROM flow_bug_signature_members WHERE signature=%s"
                      " ORDER BY kind,value", (signature,)):
        values[row["kind"]].append(row["value"])
    return values


def breaker_exclusions(db, ticket_keys: list[str]) -> set[tuple[str, str]]:
    if not ticket_keys:
        return set()
    rows = db.all("SELECT b.ticket_key,f.blocked_step FROM flow_bug_blocked_tickets b"
                  " JOIN flow_bug_tracker f ON f.signature=b.signature"
                  " WHERE f.breaker_active=true AND b.ticket_key=ANY(%s)", (ticket_keys,))
    return {(row["ticket_key"], row["blocked_step"]) for row in rows}


def set_status(db, signature: str, status: str, *, closed: bool, was_closed: bool) -> None:
    with db.transaction():
        if closed and not was_closed:
            db.run("UPDATE flow_bug_tracker SET bug_status=%s,fixed_at=coalesce(fixed_at,now()),"
                   "watch_until=coalesce(watch_until,now()+interval '24 hours'),breaker_active=false,"
                   "blocked_step='',updated_at=now() WHERE signature=%s", (status, signature))
            db.run("DELETE FROM flow_bug_blocked_tickets WHERE signature=%s", (signature,))
        elif closed:
            db.run("UPDATE flow_bug_tracker SET bug_status=%s,breaker_active=false,blocked_step='',updated_at=now()"
                   " WHERE signature=%s", (status, signature))
            db.run("DELETE FROM flow_bug_blocked_tickets WHERE signature=%s", (signature,))
        else:
            db.run("UPDATE flow_bug_tracker SET bug_status=%s,fixed_at=NULL,watch_until=NULL,updated_at=now()"
                   " WHERE signature=%s", (status, signature))


def reopened(db, signature: str, status: str) -> None:
    with db.transaction():
        db.run("UPDATE flow_bug_tracker SET bug_status=%s,fixed_at=NULL,watch_until=NULL,"
               "regression_count=regression_count+1,breaker_active=false,blocked_step='',updated_at=now()"
               " WHERE signature=%s", (status, signature))
        db.run("DELETE FROM flow_bug_blocked_tickets WHERE signature=%s", (signature,))


def intake_moved(db, signature: str) -> None:
    db.run("UPDATE flow_bug_tracker SET in_intake=true,bug_status='Intake',updated_at=now() WHERE signature=%s",
           (signature,))


def _member_counts(runs: list[dict]) -> dict[tuple[str, str], tuple[object, object, int]]:
    seen: dict[tuple[str, str], list] = defaultdict(list)
    for run in runs:
        for kind, field in _MEMBER_FIELDS.items():
            value = str(run.get(field) or "")
            if value:
                seen[(kind, value)].append(run["started_at"])
    return {key: (min(times), max(times), len(times)) for key, times in seen.items()}


def _upsert_members(db, signature: str, runs: list[dict]) -> None:
    for (kind, value), (first_seen_at, last_seen_at, occurrences) in _member_counts(runs).items():
        db.run("INSERT INTO flow_bug_signature_members"
               " (signature,kind,value,first_seen_at,last_seen_at,occurrences) VALUES (%s,%s,%s,%s,%s,%s)"
               " ON CONFLICT (signature,kind,value) DO UPDATE SET"
               " first_seen_at=LEAST(flow_bug_signature_members.first_seen_at,EXCLUDED.first_seen_at),"
               " last_seen_at=GREATEST(flow_bug_signature_members.last_seen_at,EXCLUDED.last_seen_at),"
               " occurrences=flow_bug_signature_members.occurrences+EXCLUDED.occurrences",
               (signature, kind, value, first_seen_at, last_seen_at, occurrences))


def _upsert_samples(db, signature: str, messages: list[str]) -> None:
    for position, message in enumerate(messages[:3], 1):
        db.run("INSERT INTO flow_bug_sample_messages (signature,position,message) VALUES (%s,%s,%s)"
               " ON CONFLICT (signature,position) DO NOTHING", (signature, position, message[:500]))


def _set_blocked_tickets(db, signature: str, ticket_keys: list[str], active: bool) -> None:
    if not active:
        db.run("DELETE FROM flow_bug_blocked_tickets WHERE signature=%s", (signature,))
        return
    for ticket_key in sorted(set(ticket_keys)):
        db.run("INSERT INTO flow_bug_blocked_tickets (signature,ticket_key) VALUES (%s,%s)"
               " ON CONFLICT DO NOTHING", (signature, ticket_key))


def create_tracker(db, candidate: dict, ticket_key: str, breaker_active: bool) -> None:
    signature = candidate["signature"]
    run_ids = sorted(run["id"] for run in candidate["runs"])
    with db.transaction():
        db.run("INSERT INTO flow_bug_tracker"
               " (signature,ticket_key,run_ids,repeat_count,in_intake,occurrence_count,first_seen_at,last_seen_at,"
               "tokens_wasted,cost_wasted,breaker_active,blocked_step,bug_status)"
               " VALUES (%s,%s,%s,0,false,%s,%s,%s,%s,%s,%s,%s,'')",
               (signature, ticket_key, run_ids, len(run_ids), candidate["first_seen_at"], candidate["last_seen_at"],
                candidate["tokens_used"], candidate["cost_wasted"], breaker_active,
                candidate["step"] if breaker_active else ""))
        _upsert_members(db, signature, candidate["runs"])
        _upsert_samples(db, signature, candidate["sample_messages"])
        _set_blocked_tickets(db, signature, candidate["tickets"], breaker_active)


def save_repeat(db, signature: str, metadata: dict, candidate: dict, status: str) -> None:
    with db.transaction():
        db.run("UPDATE flow_bug_tracker SET run_ids=%s,repeat_count=%s,occurrence_count=%s,first_seen_at=%s,"
               "last_seen_at=%s,tokens_wasted=%s,cost_wasted=%s,breaker_active=%s,blocked_step=%s,"
               "regression_count=%s,bug_status=%s,updated_at=now() WHERE signature=%s",
               (metadata["run_ids"], metadata["repeat_count"], metadata["occurrence_count"],
                metadata["first_seen_at"], metadata["last_seen_at"], metadata["tokens_wasted"],
                metadata["cost_wasted"], metadata["breaker_active"], metadata["blocked_step"],
                metadata["regression_count"], status, signature))
        _upsert_members(db, signature, candidate["new_runs"])
        _upsert_samples(db, signature, candidate["sample_messages"])
        _set_blocked_tickets(db, signature, candidate["tickets"], metadata["breaker_active"])


def prune_expired_members(db) -> None:
    """Remove stale signature associations after 90 days; counters and tracker identities are retained."""
    with db.transaction():
        db.run("DELETE FROM flow_bug_blocked_tickets b USING flow_bug_signature_members m"
               " WHERE m.signature=b.signature AND m.kind='ticket' AND m.value=b.ticket_key"
               " AND m.last_seen_at < now()-interval '90 days'")
        db.run("DELETE FROM flow_bug_signature_members WHERE last_seen_at < now()-interval '90 days'")
