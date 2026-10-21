"""Incidents and repeated flow-error detection.

Incidents are opened once per (kind, subject) while the condition holds, closed when it clears, and people are
alerted on open (critical and warning) and on resolve. The master cycle also uses the run history and long-lived
ticket_stuck incidents to identify repeatable TESTFLOW failures that deserve a flow-bug ticket.
"""
from __future__ import annotations

import hashlib
import re

from . import notify


_DYNAMIC_PREFIX = re.compile(
    r"^(?:(?:dev|verify|triage|intake|impact|master) worker error|review could not run|no verdict|"
    r"master cycle failed|worker error|infra|provider|timeout|failed)\s*:\s*", re.IGNORECASE)
_EXCLUDED_FAILURE = re.compile(
    r"\bquota\b|rate[ -]?limit|signed[ -]?out|\bnode\b.{0,40}\b(?:offline|down|unreachable)\b|"
    r"\b(?:missing|required)\s+(?:a\s+)?tool\b|\b(?:tool|binary|executable|command|codex|claude|git)"
    r"\s+(?:is\s+)?(?:missing|not found|not installed)\b|command not found|executable file not found|\benoent\b",
    re.IGNORECASE,
)
_TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.IGNORECASE)
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")
_NUMBER = re.compile(r"\b\d+\b")


def flow_error_pattern(error_kind: str, summary: str) -> tuple[str, str] | None:
    """Return a stable signature and readable sample for an eligible run failure.

    Quota, offline-node, and missing-tool symptoms stay incidents. For other failures the signature combines the
    adapter error kind with a normalized summary so the same checkout/provider failure groups across tickets.
    """
    raw = " ".join((summary or "").split())[:2000]
    kind = (error_kind or "").strip().lower()
    if not raw or kind in {"quota", "auth", "offline", "missing_tool", "missing-tool"}:
        return None
    if _EXCLUDED_FAILURE.search(f"{kind} {raw}"):
        return None
    sample = raw
    for _ in range(3):
        stripped = _DYNAMIC_PREFIX.sub("", sample, count=1)
        if stripped == sample:
            break
        sample = stripped
    sample = sample.strip(" .:;\n") or raw
    normalized = sample.casefold()
    normalized = _TICKET_KEY.sub("<ticket>", normalized)
    normalized = _UUID.sub("<id>", normalized)
    normalized = _PATH.sub("<path>", normalized)
    normalized = _HEX.sub("<id>", normalized)
    normalized = _NUMBER.sub("#", normalized)
    normalized = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9<>#]+", " ", normalized)).strip()
    if len(normalized) < 8 or normalized in {"failed", "error", "provider error", "timeout"}:
        return None
    digest = hashlib.sha256(f"{kind}\0{normalized}".encode()).hexdigest()[:20]
    return digest, sample[:500]


def flow_bug_candidates(ctx) -> list[dict]:
    """Find repeated TESTFLOW failures and eligible ticket_stuck incidents older than 30 minutes."""
    tracked_signatures = {row["signature"] for row in ctx.db.all("SELECT signature FROM flow_bug_tracker")}
    runs = ctx.db.all(
        "SELECT r.id, r.ticket_key, r.project, r.step, r.error_kind, r.outcome, r.summary, r.node, r.started_at,"
        " coalesce(n.release_version, '') AS release_version FROM runs r LEFT JOIN nodes n ON n.name=r.node"
        " WHERE r.project='TESTFLOW' AND r.status<>'running' AND r.outcome IN ('failed','timeout')"
        " AND coalesce(r.ended_at,r.started_at) >= now() - interval '30 minutes' ORDER BY r.started_at",
    )
    candidates: dict[str, dict] = {}

    def add_run(run: dict, incident_id: int | None = None) -> None:
        pattern = flow_error_pattern(run["error_kind"], run["summary"])
        if not pattern:
            return
        signature, sample = pattern
        item = candidates.setdefault(signature, {
            "signature": signature, "pattern": sample, "error_kind": run["error_kind"] or "unknown",
            "runs": {}, "incident_ids": set(),
        })
        item["runs"][str(run["id"])] = {
            "id": str(run["id"]), "ticket": run["ticket_key"], "step": run["step"], "node": run["node"],
            "release": run["release_version"] or "unknown",
        }
        if incident_id is not None:
            item["incident_ids"].add(incident_id)

    for run in runs:
        add_run(run)

    for incident in ctx.db.all(
        "SELECT id, subject, opened_at FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"
        " AND subject LIKE 'TESTFLOW-%' AND opened_at <= now() - interval '30 minutes'",
    ):
        run = ctx.db.one(
            "SELECT r.id, r.ticket_key, r.project, r.step, r.error_kind, r.outcome, r.summary, r.node, r.started_at,"
            " coalesce(n.release_version, '') AS release_version FROM runs r LEFT JOIN nodes n ON n.name=r.node"
            " WHERE r.project='TESTFLOW' AND r.ticket_key=%s AND r.status<>'running'"
            " AND r.outcome IN ('failed','timeout') AND r.started_at > %s - interval '1 day' AND r.started_at <= %s"
            " ORDER BY r.started_at DESC LIMIT 1",
            (incident["subject"], incident["opened_at"], incident["opened_at"]))
        if run:
            add_run(run, incident["id"])

    ready = []
    for candidate in candidates.values():
        candidate_runs = list(candidate["runs"].values())
        tickets = sorted({run["ticket"] for run in candidate_runs})
        is_repeated = len(candidate_runs) >= 3 and len(tickets) >= 2
        creation_eligible = is_repeated or bool(candidate["incident_ids"])
        if not creation_eligible and candidate["signature"] not in tracked_signatures:
            continue
        candidate["runs"] = sorted(candidate_runs, key=lambda run: run["id"])
        candidate["creation_eligible"] = creation_eligible
        candidate["tickets"] = tickets
        candidate["steps"] = sorted({run["step"] for run in candidate["runs"]})
        candidate["nodes"] = sorted({run["node"] for run in candidate["runs"]})
        candidate["releases"] = sorted({run["release"] for run in candidate["runs"]})
        candidate["incident_ids"] = sorted(candidate["incident_ids"])
        ready.append(candidate)
    return ready


def suggested_flow_fix(candidate: dict) -> str:
    """Point a flow-bug ticket at the worker or adapter most likely to own the failure."""
    text = candidate["pattern"].casefold()
    steps = candidate["steps"]
    if any(word in text for word in ("codex", "sandbox", "spawn", "adapter", "provider")):
        return "Review aiflow/v4/adapters/ and the affected worker startup path."
    if any(word in text for word in ("git", "checkout", "worktree", "branch", "clone", "fetch")):
        paths = ["aiflow/v4/worker/gitops.py"]
        paths.extend(f"aiflow/v4/worker/{step}.py" for step in steps if step in {"dev", "verify", "triage", "intake"})
        return "Review " + " and ".join(dict.fromkeys(paths)) + "."
    paths = [f"aiflow/v4/worker/{step}.py" for step in steps if step in {"dev", "verify", "triage", "intake", "impact"}]
    return "Review " + (" and ".join(dict.fromkeys(paths)) if paths else "aiflow/v4/control/ and the worker path") + "."


def open_incident(ctx, kind: str, subject: str, severity: str, title: str, detail: str = "") -> bool:
    """True when a new incident was opened (so callers alert once, not every pass)."""
    row = ctx.db.one("INSERT INTO incidents (kind, subject, severity, title, detail) VALUES (%s,%s,%s,%s,%s)"
                     " ON CONFLICT (kind, subject) WHERE closed_at IS NULL DO NOTHING RETURNING id",
                     (kind, subject, severity, title, detail))
    if row:
        ctx.events.emit("incident.opened", reason=title, outcome=severity, kind=kind, subject=subject, detail=detail)
        if severity in ("critical", "warning"):
            channels = notify.send(severity, title, detail, ctx.settings.public_url + "/cc")
            ctx.db.run("UPDATE incidents SET notified=%s WHERE id=%s", (bool(channels), row["id"]))
    return bool(row)


def close_incident(ctx, kind: str, subject: str, note: str = "") -> None:
    row = ctx.db.one("UPDATE incidents SET closed_at=now() WHERE kind=%s AND subject=%s AND closed_at IS NULL RETURNING title",
                     (kind, subject))
    if row:
        ctx.events.emit("incident.closed", reason=note or row["title"], kind=kind, subject=subject)
        notify.send("info", f"resolved: {row['title']}", note)
