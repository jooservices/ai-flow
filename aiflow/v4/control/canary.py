"""Canary release measurements and decisions for the v4 auto-deployer.

The registry's node heartbeat records the installed release, while run rows provide the outcome counters. A
release canary is one node whose desired release is overridden by ``control/releases/CANARY.json``; the normal
CURRENT release remains the fleet target until promotion. Thresholds, pending merges and merge-to-live times are
owned here so a newer merge does not restart an in-flight canary (docs/v4/canary-reach.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from .repositories import canary as canary_repo

DEFAULT_MINIMUM_AGE_S = 5 * 60
DEFAULT_MINIMUM_RUNS = 3
DEFAULT_MAXIMUM_AGE_S = 15 * 60
AGE_LIMITS_S = (60, 24 * 60 * 60)
RUN_LIMITS = (0, 1000)


@dataclass(frozen=True)
class Thresholds:
    minimum_age_s: int = DEFAULT_MINIMUM_AGE_S
    minimum_runs: int = DEFAULT_MINIMUM_RUNS
    maximum_age_s: int = DEFAULT_MAXIMUM_AGE_S
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
           installed: bool = True, *, work_available: bool = True,
           healthy: bool = True) -> dict[str, Any]:
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
        action, reason = "rollback", "canary infrastructure failure rate reached the rollback threshold"
    elif not installed:
        action, reason = "wait", "canary node has not reported the candidate release yet"
    elif (elapsed_s >= thresholds.minimum_age_s and canary_runs < thresholds.minimum_runs
          and not work_available and healthy):
        action, reason = "promote", "lack of work; judged on health checks"
    elif elapsed_s >= thresholds.maximum_age_s:
        action = "promote"
        reason = ("maximum canary time reached with too few completed runs"
                  if canary_runs < thresholds.minimum_runs else "maximum canary time reached")
    elif elapsed_s >= thresholds.minimum_age_s and canary_runs >= thresholds.minimum_runs:
        action, reason = "promote", "minimum canary time and completed run count reached"
    else:
        action, reason = "wait", "waiting for the minimum canary time and completed run count"
    result = {"action": action, "reason": reason, "elapsed_s": elapsed_s,
              "canary": canary, "baseline": baseline}
    result["cause"] = alert_cause(result)
    return result


def alert_cause(decision: dict[str, Any]) -> str:
    """Name the canary alert cause so the flow can act on it: too_few_runs, failures or slow."""
    action = str(decision.get("action") or "")
    reason = str(decision.get("reason") or "").lower()
    if action == "rollback" or "failure" in reason:
        return "failures"
    if "too few" in reason or "lack of work" in reason:
        return "too_few_runs"
    if action == "wait" or "waiting" in reason or "slow" in reason:
        return "slow"
    return "slow"


def keep_canary_target(active_target: str, newest: str) -> dict[str, str]:
    """A newer merge does not restart the canary under test; finish it, then take the newest build."""
    active = (active_target or "").strip()
    candidate = (newest or "").strip()
    if active:
        pending = candidate if candidate and candidate != active else ""
        return {"action": "continue", "target": active, "pending_newest": pending}
    return {"action": "start", "target": candidate, "pending_newest": ""}


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
    project = ctx.settings.maintenance_project
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
    cause = measurements.get("cause") or alert_cause(measurements)
    reason = str(measurements.get("reason") or "")
    if "lack of work" in reason:
        title = f"Canary {target} promoted on health checks (too few runs: no work available)"
        detail_extra = "Judged on health checks because no work was available for completed runs."
    else:
        title = f"Canary {target} promoted after the maximum observation time with too few runs"
        detail_extra = "The release was promoted after the maximum canary duration."
    detail = (f"Cause: {cause}\nReason: {reason or detail_extra}\nCanary node: {node}\n"
              f"Completed runs: {canary['runs']}\nRequired completed runs: {required_runs}\n"
              f"{detail_extra}")
    return open_incident(ctx, "canary_under_observed", target, "info", title, detail)


def get_settings(db) -> dict[str, Any]:
    row = canary_repo.fetch_settings(db)
    defaults = {"minimum_age_s": DEFAULT_MINIMUM_AGE_S, "minimum_runs": DEFAULT_MINIMUM_RUNS,
                "maximum_age_s": DEFAULT_MAXIMUM_AGE_S}
    if not row:
        return {**defaults, "updated_by": "", "updated_at": None, "defaults": dict(defaults)}
    return {"minimum_age_s": int(row["minimum_age_s"]), "minimum_runs": int(row["minimum_runs"]),
            "maximum_age_s": int(row["maximum_age_s"]), "updated_by": row["updated_by"] or "",
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "defaults": dict(defaults)}


def set_settings(db, minimum_age_s: int, minimum_runs: int, maximum_age_s: int, by: str) -> dict[str, Any]:
    if not AGE_LIMITS_S[0] <= minimum_age_s <= AGE_LIMITS_S[1]:
        raise ValueError(f"minimum age must be between {AGE_LIMITS_S[0]} and {AGE_LIMITS_S[1]} seconds")
    if not RUN_LIMITS[0] <= minimum_runs <= RUN_LIMITS[1]:
        raise ValueError(f"minimum runs must be between {RUN_LIMITS[0]} and {RUN_LIMITS[1]}")
    if not AGE_LIMITS_S[0] <= maximum_age_s <= AGE_LIMITS_S[1] or minimum_age_s > maximum_age_s:
        raise ValueError("maximum age must be at least the minimum age and within the allowed range")
    canary_repo.upsert_settings(db, minimum_age_s, minimum_runs, maximum_age_s, by[:200])
    return get_settings(db)


def thresholds_from_settings(db, failure_ratio: str | float | Decimal = "2",
                             minimum_infra_failures: int = 3) -> Thresholds:
    settings = get_settings(db)
    return Thresholds.from_values(settings["minimum_age_s"], settings["minimum_runs"],
                                  settings["maximum_age_s"], failure_ratio, minimum_infra_failures)


def note_pending_build(db, build: str, merged_at: datetime) -> None:
    """Record a newer merge that arrived while a canary is still under test."""
    build = (build or "").strip()
    if not build:
        return
    if merged_at.tzinfo is None or merged_at.utcoffset() is None:
        raise ValueError("merge timestamp must include a UTC offset")
    canary_repo.upsert_pending_build(db, build, merged_at)


def mark_build_live(db, build: str, live_at: datetime | None = None) -> dict[str, Any]:
    """Move a pending build (and the promoted target) into merge-to-live measurements."""
    build = (build or "").strip()
    when = live_at or datetime.now(timezone.utc)
    pending = canary_repo.pending_build(db, build)
    if pending:
        merged_at = pending["merged_at"]
        seconds = max(0, int((when - merged_at).total_seconds()))
        canary_repo.record_merge_live(db, build, merged_at, when, seconds)
        canary_repo.delete_pending_build(db, build)
        return {"build": build, "seconds": seconds, "recorded": True}
    canary_repo.delete_pending_build(db, build)
    return {"build": build, "seconds": None, "recorded": False}


def mark_build_and_ancestors_live(db, build: str, covered_builds: Iterable[str] = (),
                                  live_at: datetime | None = None) -> dict[str, Any]:
    """Mark a promoted build and any pending builds it contains as live at the same time."""
    when = live_at or datetime.now(timezone.utc)
    promoted = mark_build_live(db, build, when)
    covered = []
    seen = {promoted["build"]}
    for candidate in covered_builds:
        ancestor = (candidate or "").strip()
        if not ancestor or ancestor in seen:
            continue
        seen.add(ancestor)
        covered.append(mark_build_live(db, ancestor, when))
    return {**promoted, "covered_builds": covered}


def work_available_on_node(db, node: str, started_at: datetime) -> bool:
    """True when the canary node has completed or running work since the observation clock started."""
    return canary_repo.count_node_work_since(db, node, started_at) > 0


def node_is_healthy(db, node: str) -> bool:
    row = canary_repo.active_node_health(db, node)
    return bool(row and row.get("status") == "online" and row.get("accepting_work") is not False)


def control_canary_status(db, releases: Path) -> dict[str, Any]:
    """Dashboard figures: running build, canary build, pending merges and the age of the oldest."""
    from .rollout import canary_override, read_current

    now = datetime.now(timezone.utc)
    pending = canary_repo.list_pending_builds(db)
    oldest = pending[0]["merged_at"] if pending else None
    recent = canary_repo.recent_merge_live(db)
    canary = canary_override(releases)
    return {
        "running_build": read_current(releases),
        "canary_build": str(canary.get("target") or ""),
        "pending_merges": len(pending),
        "oldest_pending_age_s": (max(0, int((now - oldest).total_seconds())) if oldest else None),
        "pending": [{"build": row["build"],
                     "merged_at": row["merged_at"].isoformat() if row["merged_at"] else None}
                    for row in pending],
        "merge_to_live": [{"build": row["build"], "seconds": int(row["seconds"]),
                           "merged_at": row["merged_at"].isoformat() if row["merged_at"] else None,
                           "live_at": row["live_at"].isoformat() if row["live_at"] else None}
                          for row in recent],
        "settings": get_settings(db),
    }


def act_on_cause(ctx, *, decision: dict[str, Any], node: str, target: str,
                 required_runs: int) -> dict[str, Any]:
    """Act on a named canary cause: promote notes for too few runs; failures use record_rollback."""
    cause = decision.get("cause") or alert_cause(decision)
    if cause == "too_few_runs" and decision.get("action") == "promote":
        opened = record_promotion(ctx, target=target, node=node, measurements=decision,
                                  under_observed=True, required_runs=required_runs)
        return {"cause": cause, "acted": "recorded_under_observed", "incident_opened": opened}
    return {"cause": cause, "acted": decision.get("action") or "none", "incident_opened": False}


CI_READ_ONLY = {"checks": "read", "statuses": "read", "metadata": "read"}


def ci_state(github, repo: str, sha: str) -> str:
    """CI state of a commit from its check runs: pending | failed | ok | none (no check run at all).

    Reads through the connected GitHub App with an installation token narrowed to read-only permissions."""
    name = repo.split("/", 1)[-1]
    runs = []
    page = 1
    while True:
        data = github.api("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}", repo=name,
                          permissions=CI_READ_ONLY)
        page_runs = data.get("check_runs", [])
        runs.extend(page_runs)
        if len(page_runs) < 100:
            break
        page += 1
    if any(c.get("status") != "completed" for c in runs):
        return "pending"
    if any(c.get("conclusion") not in ("success", "skipped", "neutral") for c in runs):
        return "failed"
    return "ok" if runs else "none"
