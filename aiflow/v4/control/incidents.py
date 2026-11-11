"""Incidents and repeated flow-error detection.

Incidents are opened once per (kind, subject) while the condition holds, closed when it clears, and people are
alerted on open (critical and warning) and on resolve. The fenced master scan groups eligible failures from every
project and step into root-cause signatures.
"""
from __future__ import annotations

import hashlib
import re

from ..core.services import service_gaps
from ..core.toolchains import STACK_TOOLS, locked_version, normalize_stacks, toolchain_gaps
from . import knowledge
from . import notify
from .features.incidents import repository as incident_repository
from .repositories import flow_bugs as flow_bug_repository
from .features.tickets import ticket_repository_of


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
_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}|(?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_NUMBER = re.compile(r"\b\d+\b")


def _normalize_failure_sample(raw: str) -> tuple[str, str]:
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
    return sample[:500], normalized


def flow_error_pattern(error_kind: str, summary: str, step: str = "unknown",
                       outcome: str = "failed") -> tuple[str, str] | None:
    """Return a stable signature and readable sample for an eligible run failure.

    Quota, offline-node, and missing-tool symptoms stay incidents. A signature binds outcome and error kind to the
    normalized first summary line; affected steps are tracked as members of that root cause.
    """
    raw = " ".join(((summary or "").splitlines() or [""])[0].split())[:2000]
    kind = (error_kind or "").strip().lower()
    if not raw or kind in {"quota", "auth", "offline", "missing_tool", "missing-tool"}:
        return None
    if _EXCLUDED_FAILURE.search(f"{kind} {raw}"):
        return None
    sample, normalized = _normalize_failure_sample(raw)
    if len(normalized) < 8 or normalized in {"failed", "error", "provider error", "timeout"}:
        return None
    signature_parts = (outcome.strip().casefold() or "unknown", kind, normalized)
    digest = hashlib.sha256("\0".join(signature_parts).encode()).hexdigest()[:20]
    return digest, sample[:500]


def _candidate_run(run: dict) -> dict:
    tokens_in = int(run.get("tokens_in") or 0)
    tokens_out = int(run.get("tokens_out") or 0)
    return {"id": str(run["id"]), "ticket": run["ticket_key"], "project": run["project"],
            "step": run["step"], "outcome": run["outcome"], "error_kind": run["error_kind"] or "unknown",
            "sample": run["sample"], "node": run["node"], "account": run["account_id"],
            "provider": run["provider"] or "unknown", "release": run["release_version"] or "unknown",
            "started_at": run["started_at"], "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tokens": tokens_in + tokens_out, "cost_usd": float(run.get("cost_usd") or 0)}


def _candidate_stats(candidate: dict) -> dict:
    runs = sorted(candidate["runs"].values(), key=lambda item: item["started_at"])
    first, last = runs[0]["started_at"], runs[-1]["started_at"]
    return {**candidate, "runs": runs, "window_count": len(runs),
            "tickets": sorted({run["ticket"] for run in runs}),
            "steps": sorted({run["step"] for run in runs}),
            "providers": sorted({run["provider"] for run in runs}),
            "accounts": sorted({run["account"] for run in runs}),
            "provider_accounts": sorted({f"{run['provider']}/{run['account']}" for run in runs}),
            "nodes": sorted({run["node"] for run in runs}),
            "releases": sorted({run["release"] for run in runs}),
            "sample_messages": [run["sample"] for run in runs[:3]],
            "first_seen_at": first, "last_seen_at": last,
            "tokens_used": sum(run["tokens"] for run in runs),
            "cost_wasted": sum(run["cost_usd"] for run in runs),
            "creation_eligible": len(runs) >= 3}


def flow_bug_candidates(ctx) -> list[dict]:
    """Find signatures from all failed and timed-out runs in the rolling six-hour window."""
    tracked, runs, incidents = flow_bug_repository.candidate_data(ctx.db)
    maintenance_project = str(ctx.settings.maintenance_project or "")
    incident_opened = {}
    for incident in incidents:
        subject = str(incident["subject"])
        prefix, separator, number = subject.partition("-")
        if separator and prefix == maintenance_project and number.isdigit():
            incident_opened[subject] = incident["opened_at"]
    incident_run_ids = {
        str(run["id"]) for run in runs
        if run["project"] == maintenance_project
        and run["ticket_key"] in incident_opened
        and run["started_at"] <= incident_opened[run["ticket_key"]]
    }
    candidates: dict[str, dict] = {}
    for run in runs:
        pattern = flow_error_pattern(run["error_kind"], run["summary"], run["step"], run["outcome"])
        if not pattern:
            continue
        signature, sample = pattern
        candidate = candidates.setdefault(signature, {"signature": signature, "pattern": sample,
                                                       "error_kind": run["error_kind"] or "unknown",
                                                       "step": run["step"], "outcome": run["outcome"], "runs": {}})
        item = dict(run, sample=sample)
        candidate["runs"][str(run["id"])] = _candidate_run(item)
    ready = []
    for signature, candidate in candidates.items():
        if len(candidate["runs"]) >= 3 or signature in tracked:
            ready.append(_candidate_stats(candidate))
            continue
        eligible_incident_runs = {run_id: run for run_id, run in candidate["runs"].items()
                                  if run_id in incident_run_ids}
        if eligible_incident_runs:
            incident_candidate = {**candidate, "runs": eligible_incident_runs}
            stats = _candidate_stats(incident_candidate)
            stats["creation_eligible"] = True
            ready.append(stats)
    return ready


def flow_breaker_exclusions(ctx, candidates: list[dict]) -> set[tuple[str, str]]:
    """Return ticket-step pairs held by open flow-bug circuit breakers."""
    keys = sorted({str(item.get("key") or "") for item in candidates if item.get("key")})
    return flow_bug_repository.breaker_exclusions(ctx.db, keys)


def suggested_flow_fix(candidate: dict) -> str:
    """Point a flow-bug ticket at the worker or adapter most likely to own the failure."""
    text = candidate["pattern"].casefold()
    steps = candidate["steps"]
    if any(word in text for word in ("silent", "idle")):
        return "Review prompts/v4/dev.md, aiflow/v4/worker/dev.py and long command foreground execution."
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
    incident_id = incident_repository.insert_open_incident(ctx.db, kind, subject, severity, title, detail)
    if incident_id is not None:
        from .features.incidents.events import IncidentOpened
        from .platform.events.wiring import publish_for
        publish_for(ctx, IncidentOpened(incident_id=incident_id, kind=kind, subject=subject, severity=severity, title=title,
                                        detail=detail), deliver_now=True)
    return incident_id is not None


def close_incident(ctx, kind: str, subject: str, note: str = "") -> None:
    row = ctx.db.one("UPDATE incidents SET closed_at=now() WHERE kind=%s AND subject=%s AND closed_at IS NULL RETURNING title",
                     (kind, subject))
    if row:
        incident = ctx.db.one("SELECT id,title,detail,subject FROM incidents WHERE kind=%s AND subject=%s "
                              "ORDER BY opened_at DESC LIMIT 1", (kind, subject))
        if incident:
            item = ctx.db.one("SELECT id FROM knowledge_items WHERE source_type='incident' AND source_ref=%s "
                              "AND kind='study_case'", (str(incident["id"]),))
            if item:
                try:
                    knowledge.resolve_incident_case(ctx.db, item_id=item["id"], cause=incident["detail"] or incident["title"],
                                                    resolution=note or "The incident condition cleared.")
                except Exception as exc:  # noqa: BLE001
                    import logging
                    logging.getLogger("aiflow4.incidents").warning("incident resolution capture failed: %s", type(exc).__name__)
        ctx.events.emit("incident.closed", reason=note or row["title"], kind=kind, subject=subject)
        notify.send("info", f"resolved: {row['title']}", note, ctx=ctx,
                    alert_kind="critical_incident", needed=note or "The incident has been resolved.")


def record_run_outcome(ctx, item) -> None:
    """RunFinished reaction retained here as the incidents feature's public compatibility entry point."""
    from .features.incidents import service
    service.record_run_outcome(ctx, item)


def blocked_handoff_tickets(ctx) -> set[str]:
    from .features.incidents import service
    return service.blocked_handoff_tickets(ctx)


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
    from .features.work.constants import BACKOFF_SQL
    cooling = {step: {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": step})} for step in ("dev", "verify")}
    held = set(ctx.leases.held_keys())
    done = {r["key"] for r in ticket_repository_of(ctx).list_done_ticket_keys()}
    out = []
    for t in ticket_repository_of(ctx).workable_tickets(older_than_min):
        if t["key"] in held:
            continue
        if t["status"] == "To Do" and (t["key"] in cooling["dev"] or any(b not in done for b in t["blocked_by"])):
            continue
        if t["status"] == "In Verify" and t["key"] in cooling["verify"]:
            continue
        out.append(t)
    return out


def idle_tickets(ctx, older_than_min: float = 0.0) -> list[dict]:
    """Old queue items that still need work, including items gated by a wait, backoff or project pause."""
    held = set(ctx.leases.held_keys())
    # Tickets already covered by verify exhaustion must not also raise flow_idle (JAIF-443).
    verify_incidents = {row["subject"] for row in ctx.db.all(
        "SELECT subject FROM incidents WHERE kind='verify_no_verdict' AND closed_at IS NULL")}
    done = {r["key"] for r in ticket_repository_of(ctx).list_done_ticket_keys()}
    out = []
    for ticket in ticket_repository_of(ctx).idle_tickets(older_than_min):
        if ticket["key"] in held:
            continue
        if ticket["status"] == "In Verify" and ticket["key"] in verify_incidents:
            continue
        if ticket["status"] == "To Do" and any(blocker not in done for blocker in ticket["blocked_by"]):
            continue
        out.append(ticket)
    return out


def _idle_online_nodes(ctx) -> list[dict]:
    from .shared_accounts import NODE_ONLINE_S
    nodes = ctx.db.all("SELECT name,status,accepting_work,tools,metrics,projects,dynamic,"
                       "(last_heartbeat IS NOT NULL AND last_heartbeat > now()-make_interval(secs => %s))"
                       " AS heartbeat_fresh FROM nodes WHERE removed_at IS NULL", (NODE_ONLINE_S,))
    return [node for node in nodes
            if node["status"] == "online" and node["accepting_work"] and node["heartbeat_fresh"]]


def _idle_parked_tool_wait_reasons(ctx, ticket_keys: set[str]) -> list[str]:
    # A tool wait is a queue state with an actionable requirement; it must never collapse to "no cause found".
    parked: dict[str, list[str]] = {}
    from .tooling import requirement_is_accepted, system_action
    for wait in ctx.db.all("SELECT ticket_key,project,requirement FROM tool_waits WHERE resolved_at IS NULL"):
        if (not requirement_is_accepted(ctx, wait["project"], wait["requirement"])
                or (ticket_keys and wait["ticket_key"] not in ticket_keys)):
            continue
        parked.setdefault(wait["requirement"], []).append(wait["ticket_key"])
    reasons = []
    for requirement, keys in sorted(parked.items()):
        shown = ", ".join(sorted(set(keys))[:6])
        extra = f" and {len(set(keys)) - 6} more" if len(set(keys)) > 6 else ""
        reasons.append(f"parked tickets {shown}{extra} waiting for {requirement}; {system_action(requirement)}")
    return reasons


def _idle_retry_backoff_reasons(ctx, ticket_keys: set[str]) -> list[str]:
    # Backoff is a deliberate pause, but it still names the tickets that have no run available now.
    from .features.work.constants import BACKOFF_SQL
    cooling_tickets = {r["ticket_key"] for step in ("dev", "verify")
                       for r in ctx.db.all(BACKOFF_SQL, {"step": step})}
    cooling_tickets.intersection_update(ticket_keys)
    if not cooling_tickets:
        return []
    return ["retry backoff for " + ", ".join(sorted(cooling_tickets)[:8])]


def _idle_project_pause_reasons(repo, projects: set[str]) -> list[str]:
    # Project pause and licence limits are enforced before a lease is created.
    reasons = []
    for key in sorted(projects):
        project = repo.projects.get(key)
        if project and project.paused:
            reasons.append(f"{key} is paused in project settings")
    return reasons


def _idle_project_license_reasons(ctx, projects: set[str]) -> list[str]:
    reasons = []
    try:
        from . import licensing
        license_state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        if license_state["read_only"]:
            reasons.append(f"licence is read-only ({license_state['reason'] or 'write access is disabled'})")
        for key in sorted(projects):
            try:
                licensing.enforce_project_entitlement(ctx, key, public_key=ctx.settings.license_public_key,
                                                       state=license_state)
            except licensing.LicenseError as exc:
                reasons.append(f"licence limit stops new work for project {key}: {exc}")
    except Exception as exc:  # a broken licence read still needs an understandable cause
        reasons.append(f"licence status could not be checked ({type(exc).__name__})")
    return reasons


def _idle_compatible_nodes(ctx, serving: list[dict]) -> tuple[list[dict], list[str]]:
    from . import api as control_api, rollout
    compatible = []
    skewed = []
    for node in serving:
        verdict = rollout.node_compatible(ctx, node["name"], control_api.RELEASES)
        if verdict["compatible"]:
            compatible.append(node)
        else:
            skewed.append(f"{node['name']} ({verdict['reason']})")
    return compatible, skewed


def _idle_node_tool_or_service_block(node: dict, project, repo) -> str | None:
    gaps = toolchain_gaps(node["tools"], project.tools, repo.versions, project.stacks,
                          require_pins=bool(project.stacks))
    if gaps:
        unpinned = [gap for gap in gaps if gap in {
            tool for stack in normalize_stacks(project.stacks) for tool in STACK_TOOLS.get(stack, ())
        } and not locked_version(repo.versions, gap)]
        why = (f"versions.lock has no pin for {', '.join(unpinned)}" if unpinned
               else f"tools missing or outside their pin: {', '.join(gaps)}")
        return f"{node['name']} ({why})"
    svc = service_gaps((node["metrics"] or {}).get("services"), project.required_services)
    if svc:
        return (f"{node['name']} (test services unhealthy: "
                + ", ".join(f"{key}: {value}" for key, value in svc.items()) + ")")
    return None


def _idle_project_node_reasons(ctx, repo, project_key: str, online: list[dict]) -> list[str]:
    project = repo.projects.get(project_key)
    if not project or project.paused:
        return []
    serving = [node for node in online
               if project_key in repo.projects_for_node(node["name"], node["projects"] or [], node["dynamic"])]
    if not serving:
        return [f"no online node serves {project_key}"]
    compatible, skewed = _idle_compatible_nodes(ctx, serving)
    reasons = []
    if skewed:
        reasons.append(f"version skew blocks {project_key} on " + "; ".join(skewed))
    if not compatible:
        return reasons
    blocked = [reason for node in compatible
               if (reason := _idle_node_tool_or_service_block(node, project, repo))]
    if blocked and len(blocked) == len(compatible):
        reasons.append(f"{project_key} is gated on every node that serves it: " + "; ".join(blocked))
    return reasons


def _idle_node_reasons(ctx, repo, projects: set[str], online: list[dict]) -> list[str]:
    reasons = []
    for project_key in sorted(projects):
        reasons.extend(_idle_project_node_reasons(ctx, repo, project_key, online))
    return reasons


def _idle_account_inventory(ctx, online: list[dict]
                            ) -> tuple[list[dict], dict[str, set[str]], list[dict], list[str], list[str]]:
    accounts = ctx.db.all("SELECT a.id, a.status, a.node, a.quota_at_risk FROM accounts a")
    online_names = {node["name"] for node in online}
    # a shared account is enabled per machine (its own `node` may be empty): count every signed-in assignment
    signed_in: dict[str, set[str]] = {}
    pending: set[tuple[str, str]] = set()
    for machine in ctx.db.all("SELECT account_id, node, status FROM account_machines"):
        if machine["status"] == "signed_in":
            signed_in.setdefault(machine["account_id"], set()).add(machine["node"])
        else:
            pending.add((machine["account_id"], machine["node"]))
    ready = [account for account in accounts if account["status"] in ("idle", "working", "probing")
             and ((account["node"] in online_names and (account["id"], account["node"]) not in pending)
                  or signed_in.get(account["id"], set()) & online_names) and not ctx.leases.cooling(account["id"])]
    cooling = [account["id"] for account in accounts
               if account["status"] == "cooldown" or ctx.leases.cooling(account["id"])]
    quota_risk = [account["id"] for account in accounts if account.get("quota_at_risk")]
    return accounts, signed_in, ready, cooling, quota_risk


def _idle_quota_reasons(cooling: list[str], quota_risk: list[str]) -> list[str]:
    reasons = []
    if cooling:
        reasons.append("quota cooldown/backoff affects " + ", ".join(sorted(cooling)))
    if quota_risk:
        reasons.append("quota forecast is deferring new work for " + ", ".join(sorted(quota_risk)))
    return reasons


def _idle_account_license_reasons(ctx, accounts: list[dict], signed_in: dict[str, set[str]]) -> list[str]:
    reasons = []
    try:
        from . import licensing
        blocked_accounts: dict[str, list[str]] = {}
        for account in accounts:
            machine = account["node"] or next(iter(signed_in.get(account["id"], set())), "")
            if not machine:
                continue
            try:
                licensing.enforce_work_entitlement(ctx, account["id"], machine,
                                                   public_key=ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                blocked_accounts.setdefault(str(exc), []).append(account["id"])
        for message, ids in blocked_accounts.items():
            shown = ", ".join(sorted(ids)[:6]) + (f" and {len(ids) - 6} more" if len(ids) > 6 else "")
            reasons.append(f"licence limit stops new work for {len(ids)} AI account(s) ({shown}): {message}")
    except Exception as exc:  # preserve other known causes if a plan check cannot run
        reasons.append(f"licence work entitlement could not be checked ({type(exc).__name__})")
    return reasons


def _idle_account_readiness_reasons(accounts: list[dict], ready: list[dict], cooling: list[str],
                                   reasons: list[str]) -> list[str]:
    if ready:
        return []
    states = ", ".join(f"{account['id']}={account['status']}" for account in accounts) or "no accounts"
    if not cooling:
        return [f"no AI account is ready ({states})"]
    if not any("quota cooldown/backoff" in reason for reason in reasons):
        return [f"AI account quota cooldown/backoff leaves no account ready ({states})"]
    return []


def idle_reasons(ctx, projects: set[str], ticket_keys: set[str] | None = None) -> list[str]:
    """Why the projects with ready tickets get no worker: the concrete cause, so a person knows what to fix."""
    repo = ctx.repo()
    reasons: list[str] = []
    online = _idle_online_nodes(ctx)
    if not online:
        reasons.append("no node is online")
    ticket_keys = ticket_keys or set()
    reasons.extend(_idle_parked_tool_wait_reasons(ctx, ticket_keys))
    reasons.extend(_idle_retry_backoff_reasons(ctx, ticket_keys))
    reasons.extend(_idle_project_pause_reasons(repo, projects))
    reasons.extend(_idle_project_license_reasons(ctx, projects))
    reasons.extend(_idle_node_reasons(ctx, repo, projects, online))
    accounts, signed_in, ready, cooling, quota_risk = _idle_account_inventory(ctx, online)
    reasons.extend(_idle_quota_reasons(cooling, quota_risk))
    reasons.extend(_idle_account_license_reasons(ctx, accounts, signed_in))
    reasons.extend(_idle_account_readiness_reasons(accounts, ready, cooling, reasons))
    return reasons or ["No specific cause was observed. Checked: parked tickets and tool waits, retry backoff, "
                       "project pause, licence entitlement, node availability, version skew, toolchains, test "
                       "services, account status, quota, role health, machine assignment and active leases."]


def check_idle_flow(ctx) -> None:
    """Critical incident `flow_idle` when tickets have been ready for longer than `idle_alert_minutes` and no
    non-master run is running or has started in that time; it closes by itself when work moves again."""
    minutes = idle_minutes(ctx.repo())
    waiting = idle_tickets(ctx, minutes)
    active = ctx.db.one("SELECT 1 AS x FROM runs WHERE step <> 'master' AND (status='running'"
                        " OR started_at > now() - %s * interval '1 minute') LIMIT 1", (minutes,))
    if not waiting or active:
        close_incident(ctx, "flow_idle", "global", "work started again" if waiting else "no ticket is waiting")
        return
    projects = {t["project"] for t in waiting}
    reasons = idle_reasons(ctx, projects, {t["key"] for t in waiting})
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
