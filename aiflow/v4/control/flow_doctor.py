"""Minute-leased diagnosis and safe recovery for work that would otherwise wait for an operator.

The doctor uses the same registry, Jira adapter, node-agent operation queue and fenced leases as the normal
control plane. It records each finding and action for the Dashboard. Human actions enter the separate
operator-intervention ledger and are turned into AI Flow Bug tickets by the next doctor pass.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import role_health, shared_accounts, tooling
from .db import J
from .incidents import idle_reasons

log = logging.getLogger("aiflow4.flow_doctor")
CHECKED = ("parked tickets and tool waits", "retry backoff", "impossible dependencies", "licence", "version skew",
           "quota", "project pause", "role health", "account state", "machine state", "role account coverage")
ROLE_COVERAGE_ROLES = ("master", "intake_refiner", "verify", "dev", "release")
IMPOSSIBLE_STATUSES = {"backlog", "parked", "declined", "rejected", "won't do", "wont do"}
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}\Z")


def exclude_failed_routes(ctx, account_id: str, node: str, tickets: list[dict]) -> list[dict]:
    """Keep work away from accounts or nodes the Flow Doctor recently found failing."""
    if not tickets:
        return tickets
    avoided = {(row["ticket_key"], row["step"]) for row in ctx.db.all(
        "SELECT ticket_key,step FROM flow_doctor_route_exclusions WHERE expires_at > now()"
        " AND ((account_id<>'' AND account_id=%s) OR (node<>'' AND node=%s))", (account_id, node))}
    return [ticket for ticket in tickets if (ticket["key"], ticket["step"]) not in avoided]


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
    if "no specific cause was observed" in value or "no cause found" in value:
        return "unknown"
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
    return "unknown"


def _comment(ctx, ticket: str, text: str) -> bool:
    if not ctx.jira:
        return False
    try:
        ctx.jira.comment(ticket, text)
        return True
    except Exception as exc:  # noqa: BLE001 - retries on a later doctor pass must not stop other fixes
        log.warning("doctor could not comment on %s (%s)", ticket, type(exc).__name__)
        return False


def _retry_pending_comments(ctx, action_keys: list[str] | None = None) -> None:
    """Deliver persisted Jira explanations, keeping failures visible and retryable on the next doctor pass."""
    if not ctx.jira:
        return
    query = ("SELECT action_key,ticket_key,body FROM flow_doctor_comment_outbox WHERE delivered_at IS NULL"
             + (" AND action_key=ANY(%s)" if action_keys is not None else "") + " ORDER BY id LIMIT 100")
    rows = ctx.db.all(query, (action_keys,) if action_keys is not None else ())
    for row in rows:
        if _comment(ctx, row["ticket_key"], row["body"]):
            ctx.db.run("UPDATE flow_doctor_comment_outbox SET delivered_at=now(),attempts=attempts+1,last_error='' "
                       "WHERE action_key=%s AND delivered_at IS NULL", (row["action_key"],))
            changed = ctx.db.one("UPDATE flow_doctor_actions SET outcome='removed' WHERE action_key=%s"
                                 " AND outcome='comment_pending' RETURNING action,ticket_key",
                                 (row["action_key"],))
            if changed:
                ctx.events.emit("flow_doctor.action", ticket=changed["ticket_key"], outcome="removed",
                                reason=changed["action"], data={"action": changed["action"],
                                                                 "comment_delivered": True})
        else:
            ctx.db.run("UPDATE flow_doctor_comment_outbox SET attempts=attempts+1,"
                       "last_error='Jira comment delivery failed' WHERE action_key=%s AND delivered_at IS NULL",
                       (row["action_key"],))


def _routing_ready_nodes(ctx) -> set[str]:
    """Machines candidate routing can use now: accepting work with a fresh, online heartbeat."""
    return {row["name"] for row in ctx.db.all(
        "SELECT name FROM nodes WHERE removed_at IS NULL AND status='online' AND accepting_work=true"
        " AND last_heartbeat IS NOT NULL AND last_heartbeat > now() - make_interval(secs => %s)",
        (shared_accounts.NODE_ONLINE_S,))}


def _serving_online_nodes(ctx, project: str) -> list[dict]:
    repo = ctx.repo()
    rows = ctx.db.all("SELECT name,status,tools,metrics,projects,dynamic FROM nodes WHERE removed_at IS NULL")
    ready = _routing_ready_nodes(ctx)
    return [row for row in rows if row["name"] in ready
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
    _retry_pending_comments(ctx)
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
            action_key = f"bad-dependency:{ticket['key']}:{','.join(b['key'] for b in removed)}"
            comment = ("AI Flow · Flow doctor · dependency repaired\n"
                       f"What: Removed the blocking link to {names}.\n"
                       "Why: The blocker is parked, in Backlog, or declined and cannot complete.\n"
                       "Next: This ticket is available to normal work routing.")
            ctx.db.run("INSERT INTO flow_doctor_comment_outbox (action_key,ticket_key,body) VALUES (%s,%s,%s)"
                       " ON CONFLICT (action_key) DO NOTHING", (action_key, ticket["key"], comment))
            record_action(ctx, action_key, "drop_impossible_dependency", ticket=ticket["key"],
                          outcome="comment_pending", detail=names)
            _retry_pending_comments(ctx, [action_key])


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


def _retry_stale_run_requeues(ctx) -> None:
    """Retry Jira transitions for runs already fenced locally but not yet returned to To Do."""
    if not ctx.jira:
        return
    pending = ctx.db.all("SELECT action_key,ticket_key,node,account_id FROM flow_doctor_actions"
                         " WHERE action='fence_stale_run' AND outcome='requeue_pending'"
                         " ORDER BY created_at LIMIT 100")
    comments = []
    for row in pending:
        try:
            current = str(ctx.jira.status(row["ticket_key"]) or "").strip().casefold()
            if current in IMPOSSIBLE_STATUSES or current in {"done", "closed"}:
                changed = ctx.db.run("UPDATE flow_doctor_actions SET outcome='ticket_not_actionable'"
                                     " WHERE action_key=%s AND outcome='requeue_pending'", (row["action_key"],))
                if changed:
                    ctx.events.emit("flow_doctor.action", ticket=row["ticket_key"], node=row["node"],
                                    account=row["account_id"], outcome="ticket_not_actionable",
                                    reason=f"stale run was fenced, but Jira status is {current!r}")
                continue
            status = ctx.jira.transition_to(row["ticket_key"], "To Do")
            if status != "To Do":
                raise RuntimeError("ticket did not reach To Do")
            ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, row["ticket_key"]))
            changed = ctx.db.run("UPDATE flow_doctor_actions SET outcome='requeued'"
                                 " WHERE action_key=%s AND outcome='requeue_pending'", (row["action_key"],))
            if changed:
                ctx.events.emit("flow_doctor.action", ticket=row["ticket_key"], node=row["node"],
                                account=row["account_id"], outcome="requeued",
                                reason="Jira transition retried after the stale run was fenced")
                body = ("AI Flow · Flow doctor · worker recovered\n"
                        f"What: Fenced the silent run on {row['node']}.\n"
                        "Why: The run stopped sending heartbeats for at least 45 seconds. Its old fencing token "
                        "was retired, so it cannot write after recovery.\nNext: The ticket is back in To Do and "
                        "will be offered to a ready account.")
                comment_key = f"{row['action_key']}:comment"
                ctx.db.run("INSERT INTO flow_doctor_comment_outbox (action_key,ticket_key,body)"
                           " VALUES (%s,%s,%s) ON CONFLICT (action_key) DO NOTHING",
                           (comment_key, row["ticket_key"], body))
                comments.append(comment_key)
        except Exception as exc:  # noqa: BLE001 - pending rows remain retryable on the next minute pass
            log.warning("doctor could not requeue stale run ticket %s (%s)", row["ticket_key"], type(exc).__name__)
    if comments:
        _retry_pending_comments(ctx, comments)


def _restart_stale_runs(ctx) -> None:
    """Fence a session with no heartbeat on a live machine, requeue its ticket and restart its worker unit."""
    _retry_stale_run_requeues(ctx)
    stale = ctx.db.all("SELECT r.*,n.status AS node_status FROM runs r JOIN nodes n ON n.name=r.node"
                       " WHERE r.status='running' AND n.status='online'"
                       " AND r.heartbeat_at < now() - interval '45 seconds'")
    for run in stale:
        key = str(run["id"])
        account_id = str(run["account_id"])
        account = ctx.repo().account(run["account_id"])
        restart_worker = bool(account and _SAFE_ACCOUNT.fullmatch(account.id) and account.runs_on(run["node"]))
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
                       (account_id, account_id))
            has_active = ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1",
                                    (account_id,))
            if not has_active and restart_worker:
                payload = {"action": "restart_worker", "account": account.id, "run_id": key,
                           "reason": "the run heartbeat was absent for at least 45 seconds"}
                _queue_node_operation(ctx, run["node"], "restart-worker", payload, f"restart-worker:{key}",
                                      ticket=ticket, account=account.id,
                                      detail="old run fenced; Jira requeue will be retried until confirmed")
            record_action(ctx, f"fence-stale-run:{key}", "fence_stale_run", ticket=ticket, node=run["node"],
                          account=account_id, outcome="requeue_pending",
                          detail="run fenced; retry Jira transition until the ticket returns to To Do")
            ctx.events.emit("run.restarted", ticket=ticket, run_id=key, fence=run["fence"],
                            account=run["account_id"], node=run["node"], outcome="requeue_pending",
                            reason="worker fenced; Jira transition will retry until the ticket is requeued")
        finally:
            ctx.leases.release(ticket, recovery_id, recovery.fence)
    _retry_stale_run_requeues(ctx)


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
                            " AND status<>'running' AND outcome IN ('failed','timeout','lost','no_changes')"
                            " ORDER BY started_at DESC LIMIT 1", (ticket["key"], step))
        if not failed or not failed["ended_at"]:
            continue
        streak = ctx.db.one("WITH recent AS (SELECT outcome,row_number() OVER (ORDER BY started_at DESC) AS n"
                            " FROM runs WHERE ticket_key=%s AND step=%s AND status<>'running'"
                            " AND started_at>now()-interval '1 day')"
                            " SELECT count(*) AS n FROM recent r WHERE r.outcome IN ('failed','timeout','lost','no_changes')"
                            " AND NOT EXISTS (SELECT 1 FROM recent g WHERE g.n<r.n"
                            " AND g.outcome NOT IN ('failed','timeout','lost','no_changes'))", (ticket["key"], step))
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


ROUTE_EXCLUSION_TTL = timedelta(hours=2)


def routable_machines(ctx, account, online: set[str]) -> list[str]:
    """Machines routing would accept for a new run: signed in, online, and with account/machine capacity."""
    pending = shared_accounts.pending_nodes(ctx.db, account.id)
    return [name for name in account.machines if name in online and name not in pending
            and shared_accounts.may_take(ctx.db, account, name)[0]]


def _route_around_failed_assignment(ctx) -> None:
    """Release tickets from failed account/machine assignments when another ready route exists."""
    repo = ctx.repo()
    online = _routing_ready_nodes(ctx)
    queue = ctx.db.all("SELECT key,status,project FROM tickets WHERE (status='In Verify' OR"
                       " (status='To Do' AND 'aiflow-ready'=ANY(labels)))"
                       " AND NOT ('aiflow-hold'=ANY(labels))")
    serving_by_project = {}
    for ticket in queue:
        project = ticket["project"]
        if project not in serving_by_project:
            serving_by_project[project] = {row["name"] for row in _serving_online_nodes(ctx, project)}
    roles = {"dev": "dev", "verify": "verify", "intake": "intake_refiner", "impact": "intake_refiner"}
    for ticket in queue:
        last = ctx.db.one("SELECT id,step,account_id,node,provider,error_kind,outcome FROM runs WHERE ticket_key=%s"
                          " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        if not last or last["error_kind"] not in {"machine", "node", "offline", "tool", "tools", "auth", "network"}:
            continue
        role = roles.get(last["step"], "dev")
        # An account failure leaves the account; a machine, tool or network failure leaves the machine. The failed
        # route is then excluded for this ticket, and the claim path honours the exclusion.
        bad_account = last["account_id"] if last["error_kind"] == "auth" else ""
        bad_node = "" if last["error_kind"] == "auth" else last["node"]
        routes = []
        for a in repo.accounts:
            if role not in (a.roles or []) or a.paused or a.id == bad_account or ctx.leases.cooling(a.id):
                continue
            state = ctx.db.one("SELECT status FROM accounts WHERE id=%s", (a.id,))
            if not state or state["status"] not in ("idle", "working", "probing"):
                continue
            node = next((name for name in routable_machines(ctx, a, online)
                         if name != bad_node and name in serving_by_project[ticket["project"]]), "")
            if node:
                routes.append((a, node))
        if not routes:
            continue
        target, node = sorted(routes, key=lambda r: (r[0].provider == last.get("provider"), r[0].id == last["account_id"],
                                                     r[0].id))[0]
        action_key = f"route-alternate:{last['id']}:{target.id}:{node}"
        if record_action(ctx, action_key, "move_ticket_to_ready_account", ticket=ticket["key"],
                         account=target.id, node=node, outcome="released",
                         detail=f"last route {last['account_id']} on {last['node']} had {last['error_kind']} failure; "
                                f"ticket is released to normal routing with {target.id} on {node}"):
            ctx.db.run("INSERT INTO flow_doctor_route_exclusions (ticket_key,step,account_id,node,reason,expires_at)"
                       " VALUES (%s,%s,%s,%s,%s,now()+%s) ON CONFLICT (ticket_key,step,account_id,node)"
                       " DO UPDATE SET expires_at=EXCLUDED.expires_at,reason=EXCLUDED.reason",
                       (ticket["key"], last["step"], bad_account, bad_node,
                        f"{last['error_kind']} failure of run {last['id']}", ROUTE_EXCLUSION_TTL))
            _comment(ctx, ticket["key"], "AI Flow · Flow doctor · alternate route selected\n"
                     f"What: Released this ticket to routing on {node} for account {target.id}.\n"
                     f"Why: Its previous machine/account route ended with {last['error_kind']}.\n"
                     "Next: The ready account claims it through normal fenced routing.")


def _retry_signins(ctx) -> None:
    now = datetime.now(timezone.utc)
    for row in ctx.db.all("SELECT m.id,m.provider,m.node,m.roles,m.sign_in_status,m.sign_in_started_at,"
                          "m.sign_in_request_id,m.sign_in_url,m.sign_in_error,a.status"
                          " FROM managed_ai_accounts m JOIN accounts a ON a.id=m.id"
                          " WHERE m.removed_at IS NULL AND a.status IN ('signed_out','error')"):
        if ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1", (row["id"],)):
            continue
        if (row["sign_in_status"] == "failed"
                and str(row.get("sign_in_error") or "").startswith("Flow doctor: provider authorization is interactive")):
            continue  # don't keep reopening a provider handoff that cannot finish without a person
        if row["sign_in_status"] == "waiting" and row.get("sign_in_url"):
            # Device authorization requires a person to open the link. Retire that handoff and keep the
            # affected role moving through an already-ready account whenever one is configured.
            repo = ctx.repo()
            online = _routing_ready_nodes(ctx)
            coverage = {item["role"]: item for item in role_coverage(ctx, persist=False)}
            replacements = []
            roles = sorted(set(row.get("roles") or []) & set(coverage))
            for role in roles:
                replacement = next((repo.account(account_id)
                                    for account_id in coverage[role]["ready_accounts"]
                                    if account_id != row["id"] and repo.account(account_id)), None)
                if replacement:
                    node = next(iter(routable_machines(ctx, replacement, online)), "")
                    if node:
                        replacements.append((role, replacement, node))
            replacement_detail = "; ".join(
                f"{role}: {account.id} on {node}" for role, account, node in replacements)
            if replacements:
                detail = ("provider device authorization was waiting for a person; normal routing uses ready "
                          f"replacement account(s): {replacement_detail}")
                outcome = "replaced"
                next_step = "Work continues through the listed ready accounts. Add another provider account only if the Dashboard shows a role coverage gap."
            else:
                detail = "provider device authorization was waiting for a person; no ready replacement account is configured"
                outcome = "needs_replacement"
                next_step = "Add or enable a ready account from another provider on an online machine; the role coverage panel shows the affected role."
            ctx.db.run("UPDATE managed_ai_accounts SET sign_in_status='failed',sign_in_url='',sign_in_code='',"
                       "sign_in_error=%s,sign_in_expires_at=NULL,updated_at=now() WHERE id=%s"
                       " AND sign_in_status='waiting'", ("Flow doctor: provider authorization is interactive; "
                                                          + detail, row["id"]))
            record_action(ctx, f"signin-replacement:{row['id']}:{row.get('sign_in_request_id') or 'unknown'}",
                          "replace_interactive_signin", node=row["node"], account=row["id"], outcome=outcome,
                          detail=detail)
            _finding(ctx, f"account:{row['id']}", "account_error", detail, next_step)
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
    online = _routing_ready_nodes(ctx)
    disabled = role_health.disabled_roles(ctx.db)
    accounts = {row["id"]: row for row in ctx.db.all("SELECT id,status,provider FROM accounts")}
    holders_by_role = {
        role: [account for account in repo.accounts
               if role in (account.roles or []) and not account.paused and not account.explicit_only]
        for role in ROLE_COVERAGE_ROLES
    }
    holders = {account.id: account for role_accounts in holders_by_role.values() for account in role_accounts}
    cooldowns = ctx.leases.cooling_many(sorted(holders))
    routes_by_account = shared_accounts.routable_machines_many(ctx.db, holders.values(), online)
    route_ready = {account_id: bool(nodes) for account_id, nodes in routes_by_account.items()}
    account_ready = {
        account_id: (accounts.get(account_id, {}).get("status") in ("idle", "working", "probing")
                     and account_id not in cooldowns and route_ready[account_id])
        for account_id in holders
    }
    results = []
    for role in ROLE_COVERAGE_ROLES:
        ready = []
        expedited = []
        role_holders = holders_by_role[role]
        for account in role_holders:
            if not account_ready[account.id]:
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
            if any((account.id, role) in disabled for account in role_holders):
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
                next_step = ("No specific cause was observed. Checked: " + ", ".join(CHECKED)
                             if cause == "unknown"
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


INTERVENTION_WINDOW = timedelta(minutes=30)


def _assign_sessions(ctx, limit: int) -> None:
    """Put each new intervention into the session of its actor and source (gaps under 30 minutes), also when the
    session already has a Bug from an earlier doctor pass, so a later event never opens a second session."""
    rows = ctx.db.all("SELECT id,happened_at,actor,source FROM operator_interventions WHERE session_id IS NULL"
                      " ORDER BY happened_at,id LIMIT %s", (limit,))
    for row in rows:
        at = row["happened_at"]
        session = ctx.db.one("SELECT id,bug_ticket_key FROM operator_intervention_sessions WHERE actor=%s AND source=%s"
                             " AND started_at - %s <= %s AND last_event_at + %s >= %s"
                             " ORDER BY last_event_at DESC LIMIT 1",
                             (row["actor"], row["source"], INTERVENTION_WINDOW, at, INTERVENTION_WINDOW, at))
        if session:
            ctx.db.run("UPDATE operator_intervention_sessions SET started_at=least(started_at,%s),"
                       " last_event_at=greatest(last_event_at,%s),event_count=event_count+1 WHERE id=%s",
                       (at, at, session["id"]))
        else:
            session = ctx.db.one("INSERT INTO operator_intervention_sessions (actor,source,started_at,last_event_at,"
                                 "event_count) VALUES (%s,%s,%s,%s,1) RETURNING id,bug_ticket_key",
                                 (row["actor"], row["source"], at, at))
        ctx.db.run("UPDATE operator_interventions SET session_id=%s,bug_ticket_key=%s WHERE id=%s",
                   (session["id"], session["bug_ticket_key"], row["id"]))


def _session_evidence(ctx, session_id: int, offset: int = 0) -> tuple[list[dict], str]:
    rows = ctx.db.all("SELECT happened_at,action,target,reason,details FROM operator_interventions WHERE session_id=%s"
                      " ORDER BY happened_at,id OFFSET %s LIMIT 20", (session_id, offset))
    evidence = []
    for row in rows:
        line = f"- {row['happened_at'].isoformat()} {row['action']} {row['target']}"
        arguments = (row.get("details") or {}).get("arguments")
        if isinstance(arguments, list) and arguments:
            line += " args=" + json.dumps(arguments, ensure_ascii=False)
        evidence.append(line)
    return rows, "\n".join(evidence)


def create_intervention_bugs(ctx, limit: int = 200) -> int:
    """One Bug per actor session, not per command or file write, so an SSH session or deploy cannot flood Jira.

    Every step is retry safe: the Bug key is stored on the session as soon as Jira returns it, later events of the
    session are commented on that Bug, and a failed move to Intake is retried each pass until the Bug is claimable.
    Returns how many Bugs were created."""
    if not ctx.jira:
        return 0
    _assign_sessions(ctx, limit)
    project = getattr(ctx.settings, "maintenance_project", "JAIF")
    labels = ["flow-bug", "operator-intervention"]
    made = 0
    for session in ctx.db.all("SELECT * FROM operator_intervention_sessions WHERE bug_ticket_key='' ORDER BY id LIMIT %s",
                              (limit,)):
        rows, evidence = _session_evidence(ctx, session["id"])
        first = rows[0]
        summary = f"[Operator intervention] {session['source']} by {session['actor']}: {first['action']}"[:240]
        description = ("AI Flow detected a manual intervention. Fix the product path so this action is no longer "
                       "needed.\n\n"
                       f"When: {session['started_at'].isoformat()} ({session['event_count']} recorded action(s))\n"
                       f"Who: {session['actor']}\nWhat: {first['action']}\n"
                       f"Where: {session['source']} {first['target']}\nWhy: {first['reason'] or 'Reason not supplied'}\n"
                       f"Evidence:\n{evidence}\n\nAcceptance: reproduce the manual step, then make the flow "
                       "detect and recover from the underlying cause automatically.")
        try:
            key = ctx.jira.create_issue(project, summary, description, issuetype="Bug", labels=labels)
        except Exception as exc:  # noqa: BLE001 - the session stays without a Bug and retries on the next pass
            log.warning("could not create intervention Bug for session %s (%s)", session["id"], type(exc).__name__)
            continue
        ctx.db.run("UPDATE operator_intervention_sessions SET bug_ticket_key=%s,reported_count=%s WHERE id=%s",
                   (key, len(rows), session["id"]))
        ctx.db.run("UPDATE operator_interventions SET bug_ticket_key=%s WHERE session_id=%s",
                   (key, session["id"]))
        ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated)"
                   " VALUES (%s,%s,'Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
                   (key, key.split("-", 1)[0], labels, summary, description))
        ctx.events.emit("operator.intervention_bug", ticket=key, outcome="created",
                        reason=f"{session['event_count']} intervention(s) require a product fix",
                        data={"session_id": session["id"]})
        made += 1
    _attach_later_events(ctx, limit)
    _move_bugs_to_intake(ctx, limit)
    return made


def _attach_later_events(ctx, limit: int) -> None:
    """Comment the actions that arrived after the Bug was created on that Bug, once."""
    for session in ctx.db.all("SELECT * FROM operator_intervention_sessions WHERE bug_ticket_key<>''"
                              " AND event_count>reported_count ORDER BY id LIMIT %s", (limit,)):
        rows, evidence = _session_evidence(ctx, session["id"], int(session["reported_count"]))
        if not rows:
            continue
        try:
            ctx.jira.comment(session["bug_ticket_key"],
                             f"AI Flow · Flow doctor · more actions in the same session by {session['actor']}\n"
                             f"{evidence}")
        except Exception as exc:  # noqa: BLE001 - reported_count is unchanged, so the next pass retries
            log.warning("could not update intervention Bug %s (%s)", session["bug_ticket_key"], type(exc).__name__)
            continue
        ctx.db.run("UPDATE operator_intervention_sessions SET reported_count=reported_count+%s WHERE id=%s",
                   (len(rows), session["id"]))


def _move_bugs_to_intake(ctx, limit: int) -> None:
    """A Bug starts as Created; Intake is the state workers claim from (the normal flow-bug path)."""
    for session in ctx.db.all("SELECT id,bug_ticket_key FROM operator_intervention_sessions"
                              " WHERE bug_ticket_key<>'' AND NOT in_intake ORDER BY id LIMIT %s", (limit,)):
        key = session["bug_ticket_key"]
        try:
            status = ctx.jira.transition_to(key, "Intake")
            if status != "Intake":
                raise RuntimeError("Bug did not reach Intake")
        except Exception as exc:  # noqa: BLE001 - in_intake stays false, so the next pass retries only the move
            log.warning("could not move intervention Bug %s to Intake (%s)", key, type(exc).__name__)
            continue
        ctx.db.run("UPDATE operator_intervention_sessions SET in_intake=true WHERE id=%s", (session["id"],))
        ctx.db.run("UPDATE tickets SET status='Intake',synced_at=now() WHERE key=%s", (key,))


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
    ctx.db.run("DELETE FROM flow_doctor_route_exclusions WHERE expires_at < now()")
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
