"""Incidents and repeated flow-error detection.

Incidents are opened once per (kind, subject) while the condition holds, closed when it clears, and people are
alerted on open (critical and warning) and on resolve. The master cycle also uses the run history and long-lived
ticket_stuck incidents to identify repeatable TESTFLOW failures that deserve a flow-bug ticket.
"""
from __future__ import annotations

import hashlib
import re

from ..core.toolchains import STACK_TOOLS, locked_version, normalize_stacks, toolchain_gaps
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


# --- a flow that stands still -------------------------------------------------------------------------------
IDLE_DEFAULT_MINUTES = 15.0


def idle_minutes(repo) -> float:
    """Minutes without a started run before the flow counts as idle (policy key `idle_alert_minutes`)."""
    try:
        value = float((repo.policy or {}).get("idle_alert_minutes", IDLE_DEFAULT_MINUTES))
    except (TypeError, ValueError):
        return IDLE_DEFAULT_MINUTES
    return value if value > 0 else IDLE_DEFAULT_MINUTES


def workable_tickets(ctx, older_than_min: float = 0.0) -> list[dict]:
    """Tickets a worker could take right now ignoring who and where: To Do and aiflow-ready with every blocker
    Done, or In Verify; not on hold, not in retry backoff, not leased. `older_than_min` keeps only tickets that
    have been waiting that long (a ticket that just became ready is not a stall)."""
    from .api import BACKOFF_SQL
    cooling = {step: {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": step})} for step in ("dev", "verify")}
    held = set(ctx.leases.held_keys())
    done = {r["key"] for r in ctx.db.all("SELECT key FROM tickets WHERE status='Done'")}
    out = []
    for t in ctx.db.all(
            "SELECT key, project, status, blocked_by FROM tickets WHERE NOT ('aiflow-hold' = ANY(labels))"
            " AND ((status='To Do' AND 'aiflow-ready' = ANY(labels)) OR status='In Verify')"
            " AND coalesce(jira_updated, synced_at) <= now() - %s * interval '1 minute'", (older_than_min,)):
        if t["key"] in held:
            continue
        if t["status"] == "To Do" and (t["key"] in cooling["dev"] or any(b not in done for b in t["blocked_by"])):
            continue
        if t["status"] == "In Verify" and t["key"] in cooling["verify"]:
            continue
        out.append(t)
    return out


def idle_reasons(ctx, projects: set[str]) -> list[str]:
    """Why the projects with ready tickets get no worker: the concrete cause, so a person knows what to fix."""
    repo = ctx.repo()
    reasons: list[str] = []
    nodes = ctx.db.all("SELECT name, status, tools, projects, dynamic FROM nodes")
    online = [n for n in nodes if n["status"] == "online"]
    if not online:
        return ["no node is online"]
    for key in sorted(projects):
        project = repo.projects.get(key)
        serving = [n for n in online if key in repo.projects_for_node(n["name"], n["projects"] or [], n["dynamic"])]
        if not project or not serving:
            reasons.append(f"no online node serves {key}")
            continue
        blocked = []
        for n in serving:
            gaps = toolchain_gaps(n["tools"], project.tools, repo.versions, project.stacks,
                                  require_pins=bool(project.stacks))
            if gaps:
                unpinned = [g for g in gaps if g in {x for s in normalize_stacks(project.stacks) for x in STACK_TOOLS.get(s, ())}
                            and not locked_version(repo.versions, g)]
                why = (f"versions.lock has no pin for {', '.join(unpinned)}" if unpinned
                       else f"tools missing or outside their pin: {', '.join(gaps)}")
                blocked.append(f"{n['name']} ({why})")
        if len(blocked) == len(serving):
            reasons.append(f"{key} is gated on every node that serves it: " + "; ".join(blocked))
    accounts = ctx.db.all("SELECT a.id, a.status, a.node FROM accounts a")
    ready = [a for a in accounts if a["status"] in ("idle", "working", "probing")
             and any(n["name"] == a["node"] for n in online) and not ctx.leases.cooling(a["id"])]
    if not ready:
        states = ", ".join(f"{a['id']}={a['status']}" for a in accounts) or "no accounts"
        reasons.append(f"no AI account is ready ({states})")
    return reasons or ["no cause found: check the candidates endpoint and the master cycle"]


def check_idle_flow(ctx) -> None:
    """Critical incident `flow_idle` when tickets have been ready for longer than `idle_alert_minutes` and no
    non-master run is running or has started in that time; it closes by itself when work moves again."""
    minutes = idle_minutes(ctx.repo())
    waiting = workable_tickets(ctx, minutes)
    active = ctx.db.one("SELECT 1 AS x FROM runs WHERE step <> 'master' AND (status='running'"
                        " OR started_at > now() - %s * interval '1 minute') LIMIT 1", (minutes,))
    if not waiting or active:
        close_incident(ctx, "flow_idle", "global", "work started again" if waiting else "no ticket is waiting")
        return
    projects = {t["project"] for t in waiting}
    reasons = idle_reasons(ctx, projects)
    shown = ", ".join(t["key"] for t in waiting[:8]) + (f" and {len(waiting) - 8} more" if len(waiting) > 8 else "")
    open_incident(ctx, "flow_idle", "global", "critical",
                  f"flow is idle: {len(waiting)} tickets are ready but no run started for {minutes:g} minutes",
                  f"Ready: {shown}. Cause: " + "; ".join(reasons) + ".")


def check_stack_pins(ctx) -> None:
    """A project stack needs a pin in versions.lock for each of its tools. Missing pins keep the project from
    getting any work, so report them as a configuration error at once, naming the pin."""
    repo = ctx.repo()
    missing: set[str] = set()
    for key, project in repo.projects.items():
        for stack in normalize_stacks(project.stacks):
            for tool in STACK_TOOLS.get(stack, ()):
                if not locked_version(repo.versions, tool):
                    missing.add(f"{key}:{tool}")
    for subject in sorted(missing):
        key, tool = subject.split(":")
        open_incident(ctx, "config_pin", subject, "critical", f"versions.lock has no pin for {tool} ({key})",
                      f"Project {key} declares a stack that needs {tool}, but aiflow-control/versions.lock has no"
                      f" `tools.{tool}` entry, so no node is offered {key} tickets. Add the pin (a version or a release"
                      " line such as 8.3).")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='config_pin' AND closed_at IS NULL"):
        if i["subject"] not in missing:
            close_incident(ctx, "config_pin", i["subject"], "the pin exists")
