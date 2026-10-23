"""Minute-leased diagnosis and safe recovery for work that would otherwise wait for an operator.

The doctor uses the same registry, Jira adapter, node-agent operation queue and fenced leases as the normal
control plane. It records each finding and action for the Dashboard. Human actions enter the separate
operator-intervention ledger and are turned into AI Flow Bug tickets by the next doctor pass.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import role_health, tooling
from .db import J
from .incidents import idle_reasons

log = logging.getLogger("aiflow4.flow_doctor")
CHECKED = ("parked tickets and tool waits", "retry backoff", "impossible dependencies", "licence", "version skew",
           "quota", "project pause", "role health", "account state", "machine state", "role account coverage")
IMPOSSIBLE_STATUSES = {"backlog", "parked", "declined", "rejected", "won't do", "wont do"}
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")


def record_action(ctx, action_key: str, action: str, *, finding_id: int | None = None, ticket: str = "",
                  node: str = "", account: str = "", outcome: str = "", detail: str = "") -> bool:
    """Append one idempotent doctor action; a retry never duplicates the same repair record."""
    row = ctx.db.one("INSERT INTO flow_doctor_actions (action_key,finding_id,ticket_key,node,account_id,action,outcome,detail)"
                     " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (action_key) DO NOTHING RETURNING id",
                     (action_key[:240], finding_id, ticket[:80], node[:80], account[:100], action[:100],
                      outcome[:100], detail[:1000]))
    if row:
        ctx.events.emit("flow_doctor.action", ticket=ticket, node=node, account=account, outcome=outcome,
                        reason=action, data={"action": action, "detail": detail[:500]})
    return bool(row)


def _finding(ctx, subject: str, cause: str, detail: str, next_step: str) -> int:
    row = ctx.db.one("INSERT INTO flow_doctor_findings (subject,cause,detail,next_step) VALUES (%s,%s,%s,%s)"
                     " ON CONFLICT (subject,cause) DO UPDATE SET detail=EXCLUDED.detail,next_step=EXCLUDED.next_step,"
                     " last_seen=now(),resolved_at=NULL RETURNING id",
                     (subject[:160], cause[:100], detail[:2000], next_step[:1000]))
    return int(row["id"])


def _close_finding(ctx, subject: str, cause: str) -> None:
    ctx.db.run("UPDATE flow_doctor_findings SET resolved_at=coalesce(resolved_at,now())"
               " WHERE subject=%s AND cause=%s AND resolved_at IS NULL", (subject, cause))


def _cause(reason: str) -> str:
    value = reason.casefold()
    if "no ai account is ready" in value or "no ready account" in value:
        return "no_ready_account"
    if "parked tickets" in value or "waiting for" in value:
        return "parked_ticket"
    if "quota" in value or "cooldown" in value:
        return "quota"
    if "backoff" in value:
        return "backoff"
    if "licence" in value or "license" in value:
        return "licence"
    if "version skew" in value or "compatible release" in value:
        return "version_skew"
    if "paused" in value:
        return "project_pause"
    if "role" in value and ("off" in value or "disabled" in value or "coverage" in value):
        return "role_switched_off"
    if "block" in value or "depend" in value:
        return "impossible_dependency"
    if "node" in value or "machine" in value:
        return "machine_error"
    if "account" in value or "signed out" in value:
        return "account_error"
    return "no_ready_account"


def _comment(ctx, ticket: str, text: str) -> None:
    if not ctx.jira:
        return
    try:
        ctx.jira.comment(ticket, text)
    except Exception as exc:  # noqa: BLE001 - retries on a later doctor pass must not stop other fixes
        log.warning("doctor could not comment on %s (%s)", ticket, type(exc).__name__)


def _serving_online_nodes(ctx, project: str) -> list[dict]:
    repo = ctx.repo()
    rows = ctx.db.all("SELECT name,status,tools,metrics,projects,dynamic FROM nodes WHERE removed_at IS NULL")
    return [row for row in rows if row["status"] == "online"
            and project in repo.projects_for_node(row["name"], row["projects"] or [], row["dynamic"])]


def _fix_tool_waits(ctx) -> None:
    repo = ctx.repo()
    for wait in ctx.db.all("SELECT id,ticket_key,project,requirement,machine FROM tool_waits"
                           " WHERE resolved_at IS NULL ORDER BY created_at"):
        requirement = tooling.normalize(wait["requirement"])
        if not tooling.requirement_is_accepted(ctx, wait["project"], requirement):
            ctx.db.run("UPDATE tool_waits SET resolved_at=now(),resolution=%s WHERE id=%s AND resolved_at IS NULL",
                       ("not a validated tool or service; wait removed by Flow doctor", wait["id"]))
            text = (f"AI Flow · Flow doctor · wait resolved\nWhat: Removed the wait for {requirement}.\n"
                    "Why: This name is not a validated tool or service requirement.\n"
                    "Next: The ticket is available to normal work routing.")
            _comment(ctx, wait["ticket_key"], text)
            record_action(ctx, f"wait-invalid:{wait['id']}", "resolve_non_tool_wait", ticket=wait["ticket_key"],
                          outcome="resolved", detail=requirement)
            continue

        available = next((node for node in _serving_online_nodes(ctx, wait["project"])
                          if tooling.machine_has(node, requirement)), None)
        if available:
            ctx.db.run("UPDATE tool_waits SET resolved_at=now(),resolution=%s WHERE id=%s AND resolved_at IS NULL",
                       (f"available on {available['name']}", wait["id"]))
            _comment(ctx, wait["ticket_key"],
                     f"AI Flow · Flow doctor · wait resolved\nWhat: Cleared the wait for {requirement}.\n"
                     f"Why: {available['name']} already reports the requirement.\n"
                     "Next: Only a compatible machine is offered this ticket; other machines remain gated.")
            record_action(ctx, f"wait-present:{wait['id']}:{available['name']}", "resolve_available_tool_wait",
                          ticket=wait["ticket_key"], node=available["name"], outcome="resolved",
                          detail=f"{requirement} is present")
            continue

        # Catalogue installs are already executed by the node agent from this server-authored heartbeat plan.
        if requirement in tooling.requirement_catalogue():
            for node in _serving_online_nodes(ctx, wait["project"]):
                if tooling.machine_has(node, requirement):
                    continue
                requested = tooling.install_requests(ctx, node["name"], node, repo)
                if any(item["name"] == requirement for item in requested):
                    record_action(ctx, f"tool-install:{wait['id']}:{node['name']}", "install_catalogue_tool",
                                  ticket=wait["ticket_key"], node=node["name"], outcome="requested",
                                  detail=f"node agent will install {requirement} from its next heartbeat plan")


def _fix_impossible_dependencies(ctx) -> None:
    if not ctx.jira:
        return
    for ticket in ctx.db.all("SELECT key,blocked_by FROM tickets WHERE cardinality(blocked_by)>0"
                             " AND status NOT IN ('Done','Closed')"):
        blockers = ctx.db.all("SELECT key,status FROM tickets WHERE key=ANY(%s)", (ticket["blocked_by"],))
        impossible = [b for b in blockers if str(b["status"]).strip().casefold() in IMPOSSIBLE_STATUSES]
        if not impossible:
            continue
        try:
            read_links = getattr(ctx.jira, "get_issue_links", None) or getattr(ctx.jira, "issue_links")
            links = read_links(ticket["key"])
        except Exception as exc:  # noqa: BLE001
            log.warning("doctor could not read Jira links for %s (%s)", ticket["key"], type(exc).__name__)
            continue
        removed = []
        for blocker in impossible:
            link = next((item for item in links if item.get("direction") == "inward"
                         and item.get("other") == blocker["key"] and item.get("id", "").isdigit()), None)
            if not link:
                continue
            try:
                ctx.jira.delete_issue_link(link["id"])
            except Exception as exc:  # noqa: BLE001 - leave the mirror unchanged and retry next minute
                log.warning("doctor could not remove dependency %s -> %s (%s)", ticket["key"], blocker["key"],
                            type(exc).__name__)
                continue
            ctx.db.run("UPDATE tickets SET blocked_by=array_remove(blocked_by,%s),synced_at=now() WHERE key=%s",
                       (blocker["key"], ticket["key"]))
            removed.append(blocker)
        if removed:
            names = ", ".join(f"{b['key']} ({b['status']})" for b in removed)
            _comment(ctx, ticket["key"],
                     "AI Flow · Flow doctor · dependency repaired\n"
                     f"What: Removed the blocking link to {names}.\n"
                     "Why: The blocker is parked, in Backlog, or declined and cannot complete.\n"
                     "Next: This ticket is available to normal work routing.")
            record_action(ctx, f"bad-dependency:{ticket['key']}:{','.join(b['key'] for b in removed)}",
                          "drop_impossible_dependency", ticket=ticket["key"], outcome="removed",
                          detail=names)


def _queue_node_operation(ctx, node: str, action: str, payload: dict, action_key: str,
                          *, ticket: str = "", account: str = "", detail: str = "") -> bool:
    existing = ctx.db.one("SELECT id FROM node_operations WHERE node=%s AND action=%s AND status IN ('queued','running')"
                          " AND payload->>'action_key'=%s LIMIT 1", (node, action, action_key))
    if existing:
        return False
    operation_id = str(uuid.uuid4())
    payload = {**payload, "action_key": action_key}
    ctx.db.run("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,'flow-doctor',%s)",
               (operation_id, node, action, J(payload)))
    record_action(ctx, action_key, action, ticket=ticket, node=node, account=account, outcome="queued", detail=detail)
    return True


def _restart_stale_runs(ctx) -> None:
    """Fence a session with no heartbeat on a live machine, requeue its ticket and restart its worker unit."""
    if not ctx.jira:
        return
    stale = ctx.db.all("SELECT r.*,n.status AS node_status FROM runs r JOIN nodes n ON n.name=r.node"
                       " WHERE r.status='running' AND n.status='online'"
                       " AND r.heartbeat_at < now() - interval '45 seconds'")
    for run in stale:
        key = str(run["id"])
        account = ctx.repo().account(run["account_id"])
        if not account or not _SAFE_ACCOUNT.fullmatch(account.id) or not account.runs_on(run["node"]):
            continue
        ticket = run["ticket_key"]
        old_holder = ctx.leases.holder(ticket)
        if old_holder == f"{key}:{int(run['fence'])}" and not ctx.leases.release(ticket, key, int(run["fence"])):
            continue
        if old_holder not in (None, f"{key}:{int(run['fence'])}"):
            continue
        recovery_id = f"flow-doctor:{key}"
        recovery = ctx.leases.claim(ticket, recovery_id, max(30_000, int(ctx.settings.lease_ms)))
        if not recovery.ok:
            continue  # another worker claimed the ticket between the stale lease and doctor recovery
        try:
            updated = ctx.db.run("UPDATE runs SET status='lost',outcome='lost',error_kind='machine',ended_at=now(),"
                                 " summary='Flow doctor fenced a worker with no heartbeat'"
                                 " WHERE id=%s AND status='running'", (key,))
            if not updated:
                continue
            ctx.db.run("UPDATE accounts SET status='idle',updated_at=now() WHERE id=%s AND status='working'"
                       " AND NOT EXISTS (SELECT 1 FROM runs WHERE account_id=%s AND status='running')",
                       (account.id, account.id))
            record_action(ctx, f"fence-stale-run:{key}", "fence_stale_run", ticket=ticket, node=run["node"],
                          account=account.id, outcome="requeued", detail="fencing token retired after heartbeat stopped")
            has_active = ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1",
                                    (account.id,))
            if not has_active:
                payload = {"action": "restart_worker", "account": account.id, "run_id": key,
                           "reason": "the run heartbeat was absent for at least 45 seconds"}
                _queue_node_operation(ctx, run["node"], "restart-worker", payload, f"restart-worker:{key}",
                                      ticket=ticket, account=account.id,
                                      detail="old run fenced and ticket requeued")
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
                ctx.jira.comment(ticket,
                                 f"AI Flow · Flow doctor · worker recovered\nWhat: Fenced the silent run on {run['node']}.\n"
                                 "Why: The run stopped sending heartbeats for at least 45 seconds. Its old fencing token"
                                 " was retired, so it cannot write after recovery.\nNext: The ticket is back in To Do and"
                                 " will be offered to a ready account.")
                ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, ticket))
            except Exception as exc:  # noqa: BLE001 - keep the fenced run safe; Jira sync retries the transition
                log.warning("doctor could not requeue stale run %s (%s)", key, type(exc).__name__)
            ctx.events.emit("run.restarted", ticket=ticket, run_id=key, fence=run["fence"],
                            account=account.id, node=run["node"], outcome="requeued",
                            reason="worker heartbeat absent for at least 45 seconds")
        finally:
            ctx.leases.release(ticket, recovery_id, recovery.fence)


def _release_backoff(ctx) -> None:
    """Make an expired retry visible to normal routing and record the handoff once per failed run."""
    from .api import BACKOFF_SQL

    cooling = {step: {row["ticket_key"] for row in ctx.db.all(BACKOFF_SQL, {"step": step})}
               for step in ("dev", "verify")}
    queue = ctx.db.all("SELECT key,status FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels))"
                       " AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')")
    for ticket in queue:
        step = "verify" if ticket["status"] == "In Verify" else "dev"
        if ticket["key"] in cooling[step]:
            continue
        failed = ctx.db.one("SELECT id,account_id,node,ended_at,provider FROM runs WHERE ticket_key=%s AND step=%s"
                            " AND status<>'running' AND outcome IN ('failed','timeout','lost')"
                            " ORDER BY started_at DESC LIMIT 1", (ticket["key"], step))
        if not failed or not failed["ended_at"]:
            continue
        streak = ctx.db.one("WITH recent AS (SELECT outcome,row_number() OVER (ORDER BY started_at DESC) AS n"
                            " FROM runs WHERE ticket_key=%s AND step=%s AND status<>'running'"
                            " AND started_at>now()-interval '1 day')"
                            " SELECT count(*) AS n FROM recent r WHERE r.outcome IN ('failed','timeout','lost')"
                            " AND NOT EXISTS (SELECT 1 FROM recent g WHERE g.n<r.n"
                            " AND g.outcome NOT IN ('failed','timeout','lost'))", (ticket["key"], step))
        retry_minutes = min(30, 2 ** min(5, int((streak or {}).get("n") or 1)))
        if (datetime.now(timezone.utc) - failed["ended_at"]).total_seconds() < retry_minutes * 60:
            continue
        action_key = f"backoff-expired:{failed['id']}"
        if not record_action(ctx, action_key, "requeue_after_backoff", ticket=ticket["key"],
                             account=failed["account_id"], node=failed["node"], outcome="ready",
                             detail=f"{step} retry backoff expired; normal account and machine routing may claim it"):
            continue
        _comment(ctx, ticket["key"], "AI Flow · Flow doctor · retry released\n"
                 f"What: Requeued this ticket after its {retry_minutes}-minute retry backoff.\n"
                 "Why: The retry window has ended.\nNext: Normal account and machine routing will offer it again.")


def _route_around_failed_assignment(ctx) -> None:
    """Release tickets from failed account/machine assignments when another ready route exists."""
    repo = ctx.repo()
    online = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE status='online'")}
    queue = ctx.db.all("SELECT key,status FROM tickets WHERE status='To Do' AND 'aiflow-ready'=ANY(labels)"
                       " AND NOT ('aiflow-hold'=ANY(labels))")
    roles = {"dev": "dev", "verify": "verify", "intake": "intake_refiner", "impact": "intake_refiner"}
    for ticket in queue:
        last = ctx.db.one("SELECT id,step,account_id,node,error_kind,outcome FROM runs WHERE ticket_key=%s"
                          " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        if not last or last["error_kind"] not in {"machine", "node", "offline", "tool", "tools", "auth", "network"}:
            continue
        role = roles.get(last["step"], "dev")
        alternatives = [a for a in repo.accounts if role in (a.roles or []) and not a.paused
                        and a.id != last["account_id"] and online.intersection(a.machines)
                        and not ctx.leases.cooling(a.id)
                        and (state := ctx.db.one("SELECT status FROM accounts WHERE id=%s", (a.id,)))
                        and state["status"] in ("idle", "working", "probing")]
        if not alternatives:
            continue
        target = sorted(alternatives, key=lambda a: (a.provider == last.get("provider"), a.id))[0]
        node = next((name for name in target.machines if name in online), "")
        action_key = f"route-alternate:{last['id']}:{target.id}:{node}"
        if record_action(ctx, action_key, "move_ticket_to_ready_account", ticket=ticket["key"],
                         account=target.id, node=node, outcome="released",
                         detail=f"last route {last['account_id']} on {last['node']} had {last['error_kind']} failure; "
                                f"ticket is released to normal routing with {target.id} on {node}"):
            _comment(ctx, ticket["key"], "AI Flow · Flow doctor · alternate route selected\n"
                     f"What: Released this ticket to routing on {node} for account {target.id}.\n"
                     f"Why: Its previous machine/account route ended with {last['error_kind']}.\n"
                     "Next: The ready account claims it through normal fenced routing.")


def _retry_signins(ctx) -> None:
    now = datetime.now(timezone.utc)
    for row in ctx.db.all("SELECT m.id,m.provider,m.node,m.roles,m.sign_in_status,m.sign_in_started_at,a.status"
                          " FROM managed_ai_accounts m JOIN accounts a ON a.id=m.id"
                          " WHERE m.removed_at IS NULL AND a.status IN ('signed_out','error')"):
        if ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1", (row["id"],)):
            continue
        if row["sign_in_status"] not in ("idle", "failed") and row["sign_in_started_at"] \
                and (now - row["sign_in_started_at"]).total_seconds() < 900:
            continue
        request_id = str(uuid.uuid4())
        changed = ctx.db.one("UPDATE managed_ai_accounts SET sign_in_request_id=%s,sign_in_status='queued',"
                             "sign_in_url='',sign_in_code='',sign_in_error='',sign_in_expires_at=NULL,"
                             "sign_in_started_at=now(),updated_at=now() WHERE id=%s AND removed_at IS NULL"
                             " RETURNING id", (request_id, row["id"]))
        if changed:
            record_action(ctx, f"signin-retry:{row['id']}:{request_id}", "retry_account_sign_in",
                          node=row["node"], account=row["id"], outcome="queued",
                          detail="provider sign-in was restarted through the account's normal node-agent link")
            ctx.events.emit("account.sign_in_started", account=row["id"], provider=row["provider"],
                            node=row["node"], outcome="queued", reason="Flow doctor retried a failed account")


def role_coverage(ctx, *, persist: bool = True) -> list[dict[str, Any]]:
    """Current ready-account count by role, requiring two separate providers."""
    repo = ctx.repo()
    online = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE status='online'")}
    disabled = role_health.disabled_roles(ctx.db)
    accounts = {row["id"]: row for row in ctx.db.all("SELECT id,status,provider FROM accounts")}
    results = []
    for role in ("master", "intake_refiner", "verify", "dev"):
        holders = [acc for acc in repo.accounts if role in (acc.roles or []) and not acc.paused and not acc.explicit_only]
        ready = []
        expedited = []
        for account in holders:
            state = accounts.get(account.id, {})
            healthy = (state.get("status") in ("idle", "working", "probing")
                       and not ctx.leases.cooling(account.id) and online.intersection(account.machines))
            if not healthy:
                continue
            if (account.id, role) in disabled:
                if persist and role_health.expedite_probation(ctx.db, account.id, role):
                    expedited.append(account)
                if not role_health.probation_slot_open(ctx.db, account.id, role):
                    continue
            ready.append(account)
        providers = sorted({a.provider for a in ready})
        enough = len(ready) >= 2 and len(providers) >= 2
        reason = "" if enough else (f"{role} has {len(ready)} ready account(s) from {len(providers)} provider(s); two "
                                    "ready accounts from different providers are required")
        next_step = ("" if enough else
                     "The doctor is retrying eligible account sign-ins. If coverage stays short, add or enable an "
                     "account from another provider on an online machine, then sign it in.")
        results.append({"role": role, "ready_accounts": [a.id for a in ready], "providers": providers,
                        "ready_count": len(ready), "provider_count": len(providers), "healthy": enough,
                        "reason": reason, "next_step": next_step})
        if enough:
            if persist:
                _close_finding(ctx, f"role:{role}", "no_ready_account")
                _close_finding(ctx, f"role:{role}", "role_switched_off")
                for account in expedited:
                    record_action(ctx, f"probation-expedite:{account.id}:{role}:{datetime.now(timezone.utc).date()}",
                                  "expedite_role_probation", account=account.id, outcome="available",
                                  detail="account and machine are healthy; daily wait removed")
                    ctx.events.emit("role.probation_expedited", account=account.id, step=role,
                                    outcome="available", reason="daily probation wait removed after health recovery")
            continue
        if persist:
            finding_id = _finding(ctx, f"role:{role}", "no_ready_account", reason, next_step)
            record_action(ctx, f"role-coverage-check:{role}:{','.join(sorted(a.id for a in ready))}",
                          "check_role_coverage", finding_id=finding_id, outcome="needs_attention", detail=next_step)
            for account in expedited:
                record_action(ctx, f"probation-expedite:{account.id}:{role}:{datetime.now(timezone.utc).date()}",
                              "expedite_role_probation", finding_id=finding_id, account=account.id,
                              outcome="available", detail="machine and account are healthy; daily wait removed")
                ctx.events.emit("role.probation_expedited", account=account.id, step=role, outcome="available",
                                reason="the role is healthy enough for another easy probation ticket")
            if any((account.id, role) in disabled for account in holders):
                _finding(ctx, f"role:{role}", "role_switched_off",
                         f"{role} has a switched-off account; healthy accounts were returned to probation where possible",
                         next_step)
    return results


def _diagnose_idle_work(ctx) -> None:
    active_projects = {row["project"] for row in ctx.db.all(
        "SELECT DISTINCT project FROM runs WHERE status='running' AND step<>'master'")}
    queue = ctx.db.all("SELECT key,project,status,blocked_by FROM tickets WHERE NOT ('aiflow-hold'=ANY(labels))"
                       " AND ((status='To Do' AND 'aiflow-ready'=ANY(labels)) OR status='In Verify')")
    projects = {ticket["project"] for ticket in queue if ticket["project"] not in active_projects}
    for project in sorted(projects):
        tickets = [t for t in queue if t["project"] == project]
        keys = {t["key"] for t in tickets}
        reasons = idle_reasons(ctx, {project}, keys)
        for ticket in tickets:
            ticket_reasons = list(reasons)
            blockers = ctx.db.all("SELECT key,status FROM tickets WHERE key=ANY(%s)", (ticket["blocked_by"],)) \
                if ticket["blocked_by"] else []
            impossible = [b for b in blockers if str(b["status"]).strip().casefold() in IMPOSSIBLE_STATUSES]
            if impossible:
                ticket_reasons.append("impossible dependency on " + ", ".join(b["key"] for b in impossible))
            for reason in ticket_reasons:
                cause = _cause(reason)
                next_step = ("Flow doctor is checking every known queue gate: " + ", ".join(CHECKED)
                             if cause == "no_ready_account" and "no cause found" in reason.casefold()
                             else "Flow doctor retries safe repairs each minute; see the action log for the last attempt.")
                _finding(ctx, ticket["key"], cause, reason, next_step)


def _clear_stale_findings(ctx) -> None:
    ctx.db.run("UPDATE flow_doctor_findings SET resolved_at=now() WHERE resolved_at IS NULL"
               " AND last_seen < now() - interval '3 minutes'")


def record_operator_intervention(ctx, *, source_key: str, source: str, actor: str, action: str,
                                 target: str = "", reason: str = "", details: dict | None = None,
                                 happened_at: datetime | None = None) -> int | None:
    """Record a human action once. The next doctor pass creates a Bug ticket for it."""
    if not source_key or not actor or not action:
        raise ValueError("source key, actor and action are required")
    row = ctx.db.one("INSERT INTO operator_interventions (source_key,happened_at,source,actor,action,target,reason,details)"
                     " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_key) DO NOTHING RETURNING id",
                     (source_key[:300], happened_at or datetime.now(timezone.utc), source[:40], actor[:200],
                      action[:120], target[:300], reason[:1000] or "Reason not supplied", J(details or {})))
    if row:
        ticket_key = target if re.fullmatch(r"[A-Z][A-Z0-9]+-[0-9]+", target) else ""
        ctx.events.emit("operator.intervention", ticket=ticket_key,
                        account=actor, outcome=source, reason=action,
                        data={"intervention_id": row["id"], "target": target[:200],
                              "reason": (reason or "Reason not supplied")[:300]})
        return int(row["id"])
    existing = ctx.db.one("SELECT id FROM operator_interventions WHERE source_key=%s", (source_key[:300],))
    return int(existing["id"]) if existing else None


def create_intervention_bugs(ctx, limit: int = 30) -> int:
    if not ctx.jira:
        return 0
    made = 0
    rows = ctx.db.all("SELECT id,happened_at,source,actor,action,target,reason,details FROM operator_interventions"
                      " WHERE bug_ticket_key='' ORDER BY id LIMIT %s", (limit,))
    for row in rows:
        summary = f"[Operator intervention] {row['source']} {row['action']}"[:240]
        details = row["details"] if isinstance(row["details"], dict) else {}
        description = ("AI Flow detected a manual intervention. Fix the product path so this action is no longer "
                       "needed.\n\n"
                       f"When: {row['happened_at'].isoformat()}\nWho: {row['actor']}\nWhat: {row['action']}\n"
                       f"Where: {row['source']} {row['target']}\nWhy: {row['reason'] or 'Reason not supplied'}\n"
                       f"Evidence: {details}\n\nAcceptance: reproduce the manual step, then make the flow "
                       "detect and recover from the underlying cause automatically.")
        try:
            key = ctx.jira.create_issue(getattr(ctx.settings, "maintenance_project", "JAIF"), summary, description,
                                        issuetype="Bug", labels=["flow-bug", "operator-intervention"])
            ctx.db.run("UPDATE operator_interventions SET bug_ticket_key=%s WHERE id=%s AND bug_ticket_key=''",
                       (key, row["id"]))
            ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated)"
                       " VALUES (%s,%s,'Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
                       (key, key.split("-", 1)[0], ["flow-bug", "operator-intervention"], summary, description))
            ctx.events.emit("operator.intervention_bug", ticket=key, outcome="created",
                            reason=f"intervention {row['id']} requires a product fix",
                            data={"intervention_id": row["id"]})
            made += 1
        except Exception as exc:  # noqa: BLE001 - persisted rows retry automatically on the next pass
            log.warning("could not create intervention Bug %s (%s)", row["id"], type(exc).__name__)
    return made


def operator_intervention_summary(ctx) -> dict[str, Any]:
    counts = ctx.db.one("SELECT count(*) FILTER (WHERE happened_at >= now()-interval '24 hours') AS last_24h,"
                        " count(*) FILTER (WHERE happened_at >= now()-interval '7 days') AS last_7d"
                        " FROM operator_interventions") or {}
    rows = ctx.db.all("SELECT id,happened_at,source,actor,action,target,reason,bug_ticket_key"
                      " FROM operator_interventions ORDER BY happened_at DESC LIMIT 20")
    return {"last_24h": int(counts.get("last_24h") or 0), "last_7d": int(counts.get("last_7d") or 0),
            "recent": rows}


def dashboard(ctx) -> dict[str, Any]:
    findings = ctx.db.all("SELECT id,subject,cause,detail,next_step,first_seen,last_seen"
                          " FROM flow_doctor_findings WHERE resolved_at IS NULL ORDER BY last_seen DESC LIMIT 100")
    actions = ctx.db.all("SELECT id,ticket_key,node,account_id,action,outcome,detail,created_at"
                         " FROM flow_doctor_actions WHERE created_at >= now()-interval '7 days'"
                         " ORDER BY created_at DESC LIMIT 30")
    return {"checked": list(CHECKED), "findings": findings, "actions": actions,
            "role_coverage": role_coverage(ctx, persist=False)}


def tick(ctx) -> dict[str, int]:
    """Run a complete doctor pass. The caller holds the minute leader lease."""
    _clear_stale_findings(ctx)
    _fix_tool_waits(ctx)
    _fix_impossible_dependencies(ctx)
    _restart_stale_runs(ctx)
    _release_backoff(ctx)
    _route_around_failed_assignment(ctx)
    _retry_signins(ctx)
    _diagnose_idle_work(ctx)
    coverage = role_coverage(ctx)
    bugs = create_intervention_bugs(ctx)
    return {"role_checks": len(coverage), "intervention_bugs": bugs}
