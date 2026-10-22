"""Canary release measurements and decisions for the v4 auto-deployer.

The registry's node heartbeat records the installed release, while run rows provide the outcome counters. A
release canary is one node whose desired release is overridden by ``control/releases/CANARY.json``; the normal
CURRENT release remains the fleet target until promotion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable


@dataclass(frozen=True)
class Thresholds:
    minimum_age_s: int = 20 * 60
    minimum_runs: int = 5
    maximum_age_s: int = 60 * 60
    failure_ratio: Decimal = Decimal("2")
    minimum_infra_failures: int = 3

    @classmethod
    def from_values(cls, minimum_age_s: int, minimum_runs: int, maximum_age_s: int,
                    failure_ratio: str | float | Decimal, minimum_infra_failures: int) -> "Thresholds":
        try:
            ratio = Decimal(str(failure_ratio))
        except InvalidOperation as exc:
            raise ValueError("failure ratio must be a positive number") from exc
        if minimum_age_s < 0 or minimum_runs < 0 or maximum_age_s <= 0 or minimum_age_s > maximum_age_s:
            raise ValueError("canary ages and run counts are invalid")
        if minimum_infra_failures < 1 or not ratio.is_finite() or ratio <= 0:
            raise ValueError("canary failure thresholds must be positive")
        return cls(minimum_age_s, minimum_runs, maximum_age_s, ratio, minimum_infra_failures)


def is_infrastructure_failure(run: dict[str, Any]) -> bool:
    outcome = str(run.get("outcome") or "")
    error_kind = str(run.get("error_kind") or "")
    return outcome == "lost" or (bool(error_kind) and error_kind != "quota") or (
        outcome == "blocked" and error_kind == "infra"
    )


def summarize_runs(runs: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    rows = list(runs)
    failures = sum(is_infrastructure_failure(row) for row in rows)
    return {"runs": len(rows), "infra_failures": failures,
            "failure_rate": failures / len(rows) if rows else 0.0}


def measure_registry(db, node: str, started_at: datetime, previous_release: str) -> dict[str, Any]:
    """Count completed canary runs and the prior release's fleet baseline from the last 24 hours."""
    canary = db.one(
        "SELECT count(*) AS runs, count(*) FILTER (WHERE outcome='lost' OR"
        " (error_kind <> '' AND error_kind <> 'quota') OR (outcome='blocked' AND error_kind='infra'))"
        " AS infra_failures FROM runs WHERE node=%s AND started_at >= %s AND status <> 'running'",
        (node, started_at),
    ) or {"runs": 0, "infra_failures": 0}
    baseline = db.one(
        "SELECT count(*) AS runs, count(*) FILTER (WHERE r.outcome='lost' OR"
        " (r.error_kind <> '' AND r.error_kind <> 'quota') OR (r.outcome='blocked' AND r.error_kind='infra'))"
        " AS infra_failures FROM runs r JOIN nodes n ON n.name=r.node"
        " WHERE (n.release_version=%s OR r.node=%s) AND r.started_at >= %s - interval '24 hours'"
        " AND r.started_at < %s AND r.status <> 'running'",
        (previous_release, node, started_at, started_at),
    ) or {"runs": 0, "infra_failures": 0}

    def metrics(row: dict[str, Any]) -> dict[str, int | float]:
        count, failures = int(row["runs"] or 0), int(row["infra_failures"] or 0)
        return {"runs": count, "infra_failures": failures,
                "failure_rate": failures / count if count else 0.0}

    return {"canary": metrics(canary), "baseline": metrics(baseline)}


def decide(measurements: dict[str, Any], elapsed_s: int, thresholds: Thresholds,
           installed: bool = True) -> dict[str, Any]:
    """Return wait, rollback, or promote according to the configured canary policy."""
    canary = measurements["canary"]
    baseline = measurements["baseline"]
    # Fractions are compared as exact integers/decimals so a boundary value is deterministic.
    canary_runs = int(canary["runs"])
    canary_failures = int(canary["infra_failures"])
    baseline_runs = int(baseline["runs"])
    baseline_failures = int(baseline["infra_failures"])
    threshold_met = canary_failures >= thresholds.minimum_infra_failures and (
        Decimal(canary_failures) * Decimal(baseline_runs)
        >= thresholds.failure_ratio * Decimal(baseline_failures) * Decimal(canary_runs)
        if canary_runs else False
    )
    if threshold_met:
        action = "rollback"
        reason = "canary infrastructure failure rate reached the rollback threshold"
    elif not installed:
        action = "wait"
        reason = "canary node has not reported the candidate release yet"
    elif elapsed_s >= thresholds.maximum_age_s:
        action = "promote"
        reason = ("maximum canary time reached with too few completed runs"
                  if canary_runs < thresholds.minimum_runs else "maximum canary time reached")
    elif elapsed_s >= thresholds.minimum_age_s and canary_runs >= thresholds.minimum_runs:
        action = "promote"
        reason = "minimum canary time and completed run count reached"
    else:
        action = "wait"
        reason = "waiting for the minimum canary time and completed run count"
    return {"action": action, "reason": reason, "elapsed_s": elapsed_s,
            "canary": canary, "baseline": baseline}


def record_rollback(ctx, *, node: str, target: str, previous: str, commit: str,
                    measurements: dict[str, Any]) -> dict[str, Any]:
    """Open a canary incident and queue a Jira flow-bug for the suspected release commit."""
    from .incidents import open_incident

    canary = measurements["canary"]
    baseline = measurements["baseline"]
    # The deployer sets rollback_reason when the canary never installed the candidate (install timeout).
    reason = measurements.get("rollback_reason") or "exceeded infrastructure failure rate"
    title = f"Canary rollback: {target} on {node} {reason}"
    detail = (f"Reason: {reason}\nCandidate release: {target}\nPrevious release: {previous}\nCanary node: {node}\n"
              f"Suspected commit: {commit}\nCanary runs: {canary['runs']}\n"
              f"Canary infrastructure failures: {canary['infra_failures']}\n"
              f"Canary infrastructure failure rate: {canary['failure_rate']:.4f}\n"
              f"Previous release runs in 24 hours: {baseline['runs']}\n"
              f"Previous release infrastructure failures: {baseline['infra_failures']}\n"
              f"Previous release infrastructure failure rate: {baseline['failure_rate']:.4f}")
    opened = open_incident(ctx, "canary_rollback", target, "critical", title, detail)
    project = ctx.settings.canary_bug_project
    summary = f"[flow-bug] Canary rollback for {target} ({commit[:12]})"
    description = (f"AI Flow auto-deployer rolled back a canary release: {reason}.\n\n" + detail)
    label_hash = sha256(f"{target}\0{commit}".encode()).hexdigest()[:24]
    dedupe_label = f"aiflow4-canary-{label_hash}"
    # Queue even when open_incident returned false: the incident already exists, but its previous Jira attempt
    # may still be pending. One row per release keeps retries idempotent.
    ctx.db.run(
        "INSERT INTO canary_flow_bugs (target,node,previous,suspected_commit,project,summary,description,dedupe_label)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (target) DO UPDATE SET"
        " node=EXCLUDED.node, previous=EXCLUDED.previous, suspected_commit=EXCLUDED.suspected_commit,"
        " project=EXCLUDED.project, summary=EXCLUDED.summary, description=EXCLUDED.description,"
        " dedupe_label=EXCLUDED.dedupe_label, updated_at=now() WHERE canary_flow_bugs.ticket=''",
        (target, node, previous, commit, project, summary, description, dedupe_label),
    )
    issue = retry_pending_flow_bugs(ctx, target=target).get(target, "")
    if not issue:
        row = ctx.db.one("SELECT ticket FROM canary_flow_bugs WHERE target=%s", (target,))
        issue = str(row.get("ticket") or "") if row else ""
    return {"incident_opened": opened, "ticket": issue}


def retry_pending_flow_bugs(ctx, *, target: str | None = None) -> dict[str, str]:
    """Retry queued Jira creation without depending on a new incident opening."""
    sql = ("SELECT target,node,project,summary,description,dedupe_label,suspected_commit FROM canary_flow_bugs"
           " WHERE ticket=''" + (" AND target=%s" if target is not None else "") + " ORDER BY created_at")
    rows = ctx.db.all(sql, (target,) if target is not None else None)
    created: dict[str, str] = {}
    for row in rows:
        release = row["target"]
        if not ctx.jira:
            error = "Jira is not configured"
            ctx.db.run("UPDATE canary_flow_bugs SET last_error=%s,updated_at=now() WHERE target=%s",
                       (error, release))
            ctx.db.run("UPDATE incidents SET detail=regexp_replace(detail, E'\\nFlow-bug ticket [^\\n]*$', '') || %s"
                       " WHERE kind='canary_rollback' AND subject=%s AND closed_at IS NULL",
                       (f"\nFlow-bug ticket creation unavailable: {error}", release))
            ctx.events.emit("canary.rollback_ticket", node=row["node"], outcome="failed", reason=error)
            continue
        labels = ["flow-bug", row["dedupe_label"]]
        try:
            # A Jira create may succeed while its response or our local DB write fails. Search by a stable
            # release+commit label before creating, so a retry adopts the existing issue.
            jql = (f'project = "{row["project"]}" AND labels = "{row["dedupe_label"]}"'
                   ' AND issuetype = Bug')
            existing = next((issue for issue in ctx.jira.search(
                jql, fields=["summary", "labels", "issuetype"], page=100)
                if row["dedupe_label"] in (issue.get("fields", {}).get("labels") or [])), None)
            issue = existing["key"] if existing else ctx.jira.create_issue(
                row["project"], row["summary"], row["description"], issuetype="Bug", labels=labels)
            status = ctx.jira.status(issue)
            ctx.db.run(
                "INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                " VALUES (%s,%s,%s,'Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
                (issue, row["project"], status, labels, row["summary"], row["description"]),
            )
            from .db import J
            ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
                       " VALUES ('jira','create',%s,'aiflow4-autodeploy',true,%s,%s)",
                       (issue, status, J({"project": row["project"], "type": "Bug", "labels": labels,
                                          "commit": row["suspected_commit"], "release": release})))
            ctx.db.run("UPDATE canary_flow_bugs SET ticket=%s,last_error='',updated_at=now() WHERE target=%s",
                       (issue, release))
            ctx.db.run("UPDATE incidents SET detail=regexp_replace(detail, E'\\nFlow-bug ticket [^\\n]*$', '') || %s"
                       " WHERE kind='canary_rollback' AND subject=%s AND closed_at IS NULL",
                       (f"\nFlow-bug ticket created: {issue}", release))
            ctx.events.emit("canary.rollback_ticket", ticket=issue, node=row["node"], outcome="created",
                            reason=release)
            created[release] = issue
        except Exception as exc:  # noqa: BLE001 - rollback stays recorded if Jira is unavailable
            error = str(exc)[:300]
            ctx.db.run("UPDATE canary_flow_bugs SET last_error=%s,updated_at=now() WHERE target=%s",
                       (error, release))
            ctx.db.run("UPDATE incidents SET detail=regexp_replace(detail, E'\\nFlow-bug ticket [^\\n]*$', '') || %s"
                       " WHERE kind='canary_rollback' AND subject=%s AND closed_at IS NULL",
                       (f"\nFlow-bug ticket creation failed: {error}", release))
            ctx.events.emit("canary.rollback_ticket", node=row["node"], outcome="failed", reason=error)
    return created


def record_promotion(ctx, *, target: str, node: str, measurements: dict[str, Any], under_observed: bool,
                     required_runs: int) -> bool:
    """Record a low-priority incident note when the observation window ended without enough runs."""
    if not under_observed:
        return False
    from .incidents import open_incident

    canary = measurements["canary"]
    title = f"Canary {target} promoted after the maximum observation time with too few runs"
    detail = (f"Canary node: {node}\nCompleted runs: {canary['runs']}\n"
              f"Required completed runs: {required_runs}\n"
              "The release was promoted after the maximum canary duration.")
    return open_incident(ctx, "canary_under_observed", target, "info", title, detail)


CI_READ_ONLY = {"checks": "read", "statuses": "read", "metadata": "read"}


def ci_state(github, repo: str, sha: str) -> str:
    """CI state of a commit from its check runs: pending | failed | ok | none (no check run at all).

    Reads through the connected GitHub App with an installation token narrowed to read-only permissions."""
    name = repo.split("/", 1)[-1]
    data = github.api("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", repo=name,
                      permissions=CI_READ_ONLY)
    runs = data.get("check_runs", [])
    if any(c.get("status") != "completed" for c in runs):
        return "pending"
    if any(c.get("conclusion") not in ("success", "skipped", "neutral") for c in runs):
        return "failed"
    return "ok" if runs else "none"
