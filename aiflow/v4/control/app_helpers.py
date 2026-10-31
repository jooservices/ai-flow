"""Routes of the app_helpers feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from fastapi import Header, HTTPException, Request
from ..core import work_limits
from . import (knowledge, licensing, rollout, shared_accounts, version_release)
from .console_auth import (ConsoleUser, COOKIE_NAME, has_permission,
                           resolve_session_user,
                           verify_session)
from . import merge_queue
from . import merge_flow
from . import quota as quota_reporting
from .incidents import flow_bug_candidates, suggested_flow_fix
from . import route_deps
from .db import J
from .route_deps import h
from .features.work.constants import utcnow
from .features.work.listeners import run_finished


def _api():
    """The control app module, read when a helper runs so its tunable constants may be patched by tests."""
    from . import api  # noqa: PLC0415 - api imports this module
    return api


def install(app, ctx, deps) -> SimpleNamespace:
    def _releases():
        from .api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it
        return RELEASES

    from .api import github_gateway, log  # noqa: PLC0415 - api imports this module
    _identity: list[tuple[str, str, str]] = []

    def bot_identity() -> tuple[str, str]:
        github = github_gateway(ctx)
        if not github:
            return "aiflow4", "aiflow4@localhost"
        app_id = str((getattr(github, "meta", {}) or {}).get("id") or "")
        if not _identity or _identity[0][0] != app_id:
            name, email = github.bot_identity()
            _identity[:] = [(app_id, name, email)]
        return _identity[0][1], _identity[0][2]

    # --- auth -----------------------------------------------------------------------------------------------
    def node_auth(authorization: str = Header(default="")) -> str:
        return route_deps.node_auth(ctx, authorization)

    def release_reader(authorization: str = Header(default="")) -> str:
        """A node token, or an unexpired, unconsumed join token so a new machine can fetch the signed release."""
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            row = ctx.db.one("SELECT node_name FROM node_join_tokens WHERE token_hash=%s AND used_at IS NULL"
                             " AND expires_at > now()", (h(token),))
            if row:
                return row["node_name"]
        return node_auth(authorization)

    def reviewer_auth(request: Request, authorization: str = Header(default="")) -> str:
        """The legacy reviewer bearer token, or a console session whose roles grant `review.actions`."""
        configured = ctx.settings.reviewer_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            return "reviewer"
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user:
            ticket_key = str(request.path_params.get("key", ""))
            row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket_key,)) if ticket_key else None
            in_scope = (user.project_keys is None or not row or row["project"] in user.project_keys)
            if has_permission(user.effective_roles, "review.actions") and in_scope:
                return user.subject
            audit("console", "access_denied", ticket_key, "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "reviewer actions are not configured")
        raise HTTPException(401, "reviewer authorization required", headers={"WWW-Authenticate": "Bearer"})

    def owner_auth(request: Request, authorization: str = Header(default="")) -> str:
        def writable() -> None:
            try:
                licensing.require_writable(ctx.db, ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None

        configured = ctx.settings.owner_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            writable()
            return _api().OWNER_TOKEN_PRINCIPAL
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user and has_permission(user.effective_roles, "knowledge.approve"):
            writable()
            return user.subject
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "owner actions are not configured")
        if user:
            audit("aiflow-control", "knowledge_approve", "", "", None, user.subject, False,
                  "not available for your role")
            raise HTTPException(403, "not available for your role")
        raise HTTPException(401, "owner authorization required", headers={"WWW-Authenticate": "Bearer"})

    def deploy_auth(authorization: str = Header(default="")) -> str:
        """The deploy scripts on the control machine sign in with the owner token (a bearer token, never a cookie)."""
        configured = ctx.settings.owner_token
        if not configured:
            raise HTTPException(503, "deploy access is not configured")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or not secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            raise HTTPException(401, "deploy authorization required", headers={"WWW-Authenticate": "Bearer"})
        return "deploy"

    def audit(target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
              result: str, payload: dict | None = None) -> None:
        route_deps.audit(ctx, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def audit_in(connection, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str,
                 accepted: bool, result: str, payload: dict | None = None) -> None:
        route_deps.audit_in(connection, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def console_user(request: Request, permissions: set[str] | str | None, target: str = "console", action: str = "read",
                     node: str = "") -> ConsoleUser:
        return route_deps.console_user(ctx, request, permissions, target, action, node)

    def optional_console_user(request: Request) -> ConsoleUser | None:
        return route_deps.optional_console_user(ctx, request)

    def require_capacity(resource: str, count: int | None = None, *, connection=None) -> dict:
        try:
            return licensing.require_capacity(ctx, resource, count, public_key=ctx.settings.license_public_key,
                                              connection=connection)
        except licensing.LicenseError as exc:
            raise HTTPException(409, str(exc)) from None

    def run_row(run_id: str) -> dict:
        return route_deps.run_row(ctx, run_id)

    def fenced(run_id: str, fence: int, target: str, action: str, payload: dict | None = None) -> dict:
        return route_deps.fenced(ctx, run_id, fence, target, action, payload)

    def master_run(run_id: str, fence: int, node: str, action: str, payload: dict | None = None) -> dict:
        run = fenced(run_id, fence, "master", action, payload)
        if run["step"] != "master":
            raise HTTPException(403, "only a master run can use this endpoint")
        if run["node"] != node:
            raise HTTPException(403, "master run belongs to another node")
        return run

    def reviewer_run(key: str, statuses: tuple[str, ...] = ("Human review",),
                     allowed_kinds: tuple[str, ...] | None = None) -> tuple[dict, str, int, int]:
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        ticket = ctx.db.one("SELECT key, project, status, labels, summary, blocked_by FROM tickets WHERE key=%s", (key,))
        if not ticket:
            raise HTTPException(404, "unknown ticket")
        if ticket["status"] not in statuses:
            raise HTTPException(409, "ticket is no longer waiting for this review action")
        if allowed_kinds is not None and deps.late.github_fenced_routes.current_review_item_kind(ticket) not in allowed_kinds:
            raise HTTPException(409, "this action does not apply to the current human review item")
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        pr_run = ctx.db.one("SELECT pr_url,details FROM runs WHERE ticket_key=%s AND pr_url<>''"
                            " ORDER BY started_at DESC LIMIT 1", (key,))
        match = re.search(r"/pull/(\d+)(?:/|$)", (pr_run or {}).get("pr_url", ""))
        number = int(match.group(1)) if match else 0
        if number:
            repository = str(((pr_run or {}).get("details") or {}).get("repository") or "")
            project = ctx.repo().projects.get(ticket["project"])
            repository = repository or (repository_for_run(pr_run or {}, project) if project else "")
            if not project or not repository:
                raise HTTPException(404, "project repository is not configured")
            github_gateway(ctx, repository)
        run_id = str(uuid.uuid4())
        claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
        if not claim.ok:
            raise HTTPException(409, "ticket is already being changed")
        try:
            run_details = {"repository": str(((pr_run or {}).get("details") or {}).get("repository") or "")}
            ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash,details)"
                        " VALUES (%s,%s,%s,'qa','reviewer-console','human','control',%s,'',%s)",
                        (run_id, key, ticket["project"], claim.fence, J(run_details)))
        except Exception:
            ctx.leases.release(key, run_id, claim.fence)
            raise
        return ticket, run_id, claim.fence, number

    def finish_reviewer_run(key: str, run_id: str, fence: int, outcome: str, summary: str) -> None:
        ctx.db.run("UPDATE runs SET status='finished', outcome=%s, summary=%s, ended_at=now() WHERE id=%s",
                   (outcome, summary[:2000], run_id))
        try:
            knowledge.capture_reviewer_run(ctx.db, run_id, ctx.settings.jira_site, ticket_key=key, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - learning must not block a review action
            log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
        ctx.leases.release(key, run_id, fence)
        run_finished(ctx, run_id=run_id, ticket_key=key, project=key.split("-", 1)[0], step="qa", fence=fence,
                     account="reviewer-console", provider="human", node="control", outcome=outcome, reason=summary[:200])

    def reviewer_comment(ticket: str, run_id: str, fence: int, number: int, text: str,
                         source: str = "human-review") -> None:
        """Mirror a reviewer action to Jira and, when the ticket has a PR, its PR conversation."""
        jira_run = fenced(run_id, fence, "jira", "comment", {"chars": len(text)})
        comment_id = ctx.jira.comment(ticket, text)
        audit("jira", "comment", ticket, run_id, fence, jira_run["account_id"], True, comment_id,
              {"source": source, "chars": len(text)})
        if number:
            github_run = fenced(run_id, fence, "github", "comment", {"number": number})
            project = ctx.repo().projects.get(github_run["project"])
            if not project:
                raise HTTPException(404, "project is not configured")
            repository = repository_for_run(github_run, project)
            gateway = github_gateway(ctx, repository)
            gateway.comment(repository.split("/", 1)[-1], number, text)
            audit("github", "comment", ticket, run_id, fence, github_run["account_id"], True, "ok",
                  {"source": source, "number": number, "repo": repository})

    def reviewer_transition(ticket: str, run_id: str, fence: int, target: str, reason: str) -> str:
        transition_run = fenced(run_id, fence, "jira", "transition", {"to": target})
        before = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (ticket,))
        status = ctx.jira.transition_to(ticket, target)
        ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(done_at,now())"
                   " ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, ticket))
        merge_flow.record_move(ctx, ticket, (before or {}).get("status", ""), status, merge_flow.PERSON, reason)
        audit("jira", "transition", ticket, run_id, fence, transition_run["account_id"], True, status,
              {"to": target, "source": "human-review"})
        ctx.events.emit("jira.transition", ticket=ticket, project=transition_run["project"], run_id=run_id,
                        fence=fence, account="reviewer-console", node="control", outcome=status, reason=reason)
        return status

    def reviewer_labels(ticket: str, run_id: str, fence: int, add: tuple[str, ...] = (),
                        remove: tuple[str, ...] = ()) -> None:
        if not add and not remove:
            return
        fields_run = fenced(run_id, fence, "jira", "fields", {"add": list(add), "remove": list(remove)})
        for label in add:
            ctx.jira.add_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s=ANY(labels) THEN labels"
                       " ELSE array_append(labels,%s) END WHERE key=%s", (label, label, ticket))
        for label in remove:
            ctx.jira.remove_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket))
        audit("jira", "fields", ticket, run_id, fence, fields_run["account_id"], True, "review labels updated",
              {"source": "human-review", "add": list(add), "remove": list(remove)})

    def record_reviewer_action(ticket: dict, run_id: str, fence: int, action: str, text: str = "") -> None:
        latest_dev = ctx.db.one("SELECT step,outcome FROM runs WHERE ticket_key=%s AND step='dev'"
                                " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        kind = deps.late.github_fenced_routes.review_item_kind(ticket, latest_dev)
        audit("human-review", action, ticket["key"], run_id, fence, "reviewer-console", True, "accepted",
              {"kind": kind, "text": text[:4000]})
        ctx.events.emit("review.action", ticket=ticket["key"], project=ticket["project"], step="qa",
                        run_id=run_id, fence=fence, account="reviewer-console", node="control", outcome=action,
                        action=action, kind=kind, text=text[:4000])

    def repository_for_run(run: dict[str, Any], project=None) -> str:
        """Use the repository captured when the run was claimed, with a legacy settings fallback."""
        saved = str(((run.get("details") or {}).get("repository") or ""))
        if saved:
            return saved
        project = project or ctx.repo().projects.get(run.get("project"))
        if not project:
            return ""
        ticket = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (run.get("ticket_key"),)) or {}
        ticket["epic"] = deps.late.projects_routes.ticket_epic(ticket)
        return project.repo_for(ticket)

    def settings_for_run(run: dict[str, Any], project) -> dict[str, Any]:
        settings = (run.get("details") or {}).get("repository_settings")
        if isinstance(settings, dict) and settings.get("repo"):
            return settings
        return project.settings_for_repo(repository_for_run(run, project))

    def matches_run_repository(repository: str, requested: str) -> bool:
        requested = requested.strip().lower()
        return requested in {repository.lower(), repository.split("/", 1)[-1].lower()}

    def merge_command(run: dict, number: int, title: str = "", method: str = "squash", node: str = "") -> dict:
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        repo_name = repository.split("/", 1)[-1]
        res = github.merge_pr(repo_name, number, title, method)
        ok = bool(res.get("merged"))
        audit("github", "merge", run["ticket_key"], str(run["id"]), run["fence"], run["account_id"], ok,
              res.get("sha") or res.get("message", ""), {"number": number, "method": method,
                                                             "repo": repository})
        ctx.events.emit("github.merge", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                        account=run["account_id"], node=node or run["node"],
                        outcome="merged" if ok else "refused", reason=res.get("message", "")[:200])
        return res

    def queued_merge(run: dict, number: int, title: str, method: str, node: str) -> dict:
        """Merge a human-approved pull request through the merge queue, like a verify run does.

        The click waits (up to _api().HUMAN_QUEUE_WAIT_S) for its turn behind pull requests that are ready earlier; a
        branch that had to be brought up to date is not merged on this click (its checks must run on the new head)
        and the reviewer approves again."""
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        deadline = time.monotonic() + _api().HUMAN_QUEUE_WAIT_S
        while True:
            res = merge_queue.merge_next(ctx.db, github_gateway(ctx, repository), repository, number,
                                         run["ticket_key"], str(run["id"]),
                                         lambda: merge_command(run, number, title, method, node))
            if not res.get("queued") or time.monotonic() >= deadline:
                break
            time.sleep(_api().HUMAN_QUEUE_POLL_S)
        if res.get("queued"):
            merge_queue.leave(ctx.db, repository, number)
        if not res.get("merged") and not res.get("message"):
            res["message"] = "the merge was refused"
        return res

    def master_budget_state() -> dict[str, Any]:
        setting = ctx.db.one("SELECT daily_budget_minutes FROM master_settings WHERE singleton=true") or {
            "daily_budget_minutes": _api().DEFAULT_MASTER_DAILY_BUDGET_MINUTES}
        budget = int(setting["daily_budget_minutes"])
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        usage = ctx.db.one(
            "SELECT coalesce(sum(coalesce(nullif(r.details->>'model_time_s','')::numeric,"
            " extract(epoch FROM (coalesce(r.ended_at,now())-r.started_at))) / 60),0) AS used_minutes"
            " FROM runs r WHERE r.step='master' AND r.started_at >= %s", (day_start,)) or {}
        used = max(0.0, float(usage.get("used_minutes") or 0))
        remaining = max(0, int(budget * 60 - used * 60))
        return {"daily_budget_minutes": budget, "used_minutes": round(used, 1),
                "remaining_minutes": round(max(0, budget - used), 1), "remaining_seconds": remaining,
                "enabled": budget > 0 and remaining > 0}

    def account_master_paused(account_id: str) -> bool:
        return bool(ctx.db.one("SELECT 1 AS paused FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account_id,)))

    def mark_machine_auth_failure(account_id: str, node: str, summary: str) -> None:
        """Require sign-in again on the failing machine while keeping other assignments usable."""
        acc = ctx.repo().account(account_id)
        other_machine_ready = shared_accounts.mark_pending(ctx.db, acc, node) if acc else False
        active = bool(ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1",
                                 (account_id,)))
        cooling = ctx.leases.cooling(account_id)
        status = "cooldown" if cooling else "working" if active else "idle" if other_machine_ready else "signed_out"
        ctx.db.run("UPDATE accounts SET status=%s,last_error=%s,updated_at=now(),"
                   "idle_since=CASE WHEN %s='idle' THEN coalesce(idle_since,now()) ELSE NULL END WHERE id=%s",
                   (status, summary[:300], status, account_id))

    def quota_forecast_at_risk(account_id: str) -> bool:
        """Read the latest watchdog forecast without rebuilding weeks of run and probe history per poll."""
        row = ctx.db.one("SELECT quota_at_risk FROM accounts WHERE id=%s", (account_id,))
        return bool(row and row["quota_at_risk"])

    def refresh_quota_forecast(account_id: str, provider: str, quota: dict) -> dict:
        """Refresh one account's saved forecast after it reports new quota data."""
        awareness = quota_reporting.account_assessment(ctx.db, account_id, provider, quota or {})
        ctx.db.run("UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s",
                   (bool(awareness.get("at_risk")), J(awareness), account_id))
        return awareness

    def quota_peer_accounts(account_id: str, roles: set[str] | None = None) -> list[dict]:
        """Ready same-provider accounts that can serve at least one of this account's workflow roles."""
        account_row = ctx.db.one("SELECT provider FROM accounts WHERE id=%s", (account_id,))
        if not account_row:
            return []
        peers = ctx.db.all(
            "SELECT a.id,a.roles,a.status,a.quota_at_risk FROM accounts a"
            " WHERE a.provider=%s AND a.id<>%s AND a.status NOT IN ('cooldown','signed_out','error') ORDER BY a.id",
            (account_row["provider"], account_id))
        online = shared_accounts.online_nodes(ctx.db)
        available: list[dict] = []
        for peer in peers:
            account = ctx.repo().account(peer["id"])
            peer["nodes"] = ([machine for machine in account.machines if machine in online
                               and rollout.node_compatible(ctx, machine, _releases())["compatible"]
                               and shared_accounts.may_take(ctx.db, account, machine, as_peer=True)[0]]
                              if account else [])
            if not account or account.paused or not peer["nodes"]:
                continue
            peer_roles = set(peer.get("roles") or ["dev", "verify"])
            if roles and not roles.intersection(peer_roles):
                continue
            if roles == {"master"} and peer["status"] not in ("idle", "working", "probing"):
                continue
            if peer["quota_at_risk"] or ctx.leases.cooling(peer["id"]) or account_master_paused(peer["id"]):
                continue
            available.append(peer)
        return available

    def account_quota_at_risk(account_id: str, roles: set[str] | None = None) -> bool:
        """Defer a risky account only while another ready account of the same provider can take its role."""
        if not quota_forecast_at_risk(account_id):
            return False
        return bool(quota_peer_accounts(account_id, roles))

    def quota_peer_release_candidates(account_id: str) -> set[tuple[str, str, str]]:
        """Release work a ready peer can actually claim for this quota-risky account."""
        if not quota_forecast_at_risk(account_id):
            return set()
        available: set[tuple[str, str, str]] = set()
        for peer in quota_peer_accounts(account_id, {"master", "release"}):
            for node in peer["nodes"]:
                if not rollout.node_compatible(ctx, node, _releases())["compatible"]:
                    continue
                for candidate in version_release.candidates(ctx, peer["id"], node):
                    available.add((candidate["project"], str(candidate["version_id"]),
                                   str(candidate.get("repository") or "")))
        return available

    def master_snapshot() -> dict:
        now = utcnow()
        repo = ctx.repo()
        incidents = ctx.db.all("SELECT id, opened_at, severity, kind, subject, title, detail FROM incidents"
                               " WHERE closed_at IS NULL ORDER BY opened_at")
        needs_owner = ctx.db.all("SELECT key, project, summary, description, priority, labels, jira_updated,"
                                 " ('owner-escalated'=ANY(labels)) AS owner_escalated FROM tickets"
                                 " WHERE status='Needs owner'"
                                 " ORDER BY priority, jira_created NULLS LAST, key")
        running = ctx.db.all("SELECT id, ticket_key, project, step, account_id, provider, model, node, fence, started_at"
                             " FROM runs WHERE status='running' AND step<>'master' ORDER BY started_at")
        clocks = []
        for run in running:
            project = repo.projects.get(run["project"])
            limits = project.work_minutes() if project else dict(work_limits.DEFAULTS)
            elapsed = max(0, (now - run["started_at"]).total_seconds() / 60)
            clocks.append({"run_id": str(run["id"]), "ticket": run["ticket_key"], "project": run["project"],
                           "step": run["step"], "account": run["account_id"], "provider": run["provider"],
                           "model": run["model"], "node": run["node"], "fence": run["fence"],
                           "started_at": run["started_at"].isoformat(), "elapsed_minutes": round(elapsed, 1),
                           "work_minutes": limits,
                           "passed": [name for name in ("target", "warn", "hard") if elapsed >= limits[name]]})
        offline_nodes = ctx.db.all("SELECT name, last_heartbeat FROM nodes WHERE status='offline' ORDER BY name")
        quota_incidents = [i for i in incidents if i["kind"] in ("account_quota", "quota_high")]
        high_subjects = {i["subject"].split(":", 1)[0] for i in quota_incidents if i["kind"] == "quota_high"}
        quota_accounts = []
        for account in ctx.db.all("SELECT id, provider, node, status, cooldown_until, last_error, quota FROM accounts"):
            cooling = ctx.leases.cooling(account["id"])
            pause = ctx.db.one("SELECT paused_until, reason FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account["id"],))
            if (account["status"] in ("cooldown", "signed_out", "error") or cooling
                    or account["id"] in high_subjects or pause):
                quota_accounts.append({**account, "cooldown": {"reason": cooling[0], "seconds": cooling[1]}
                                      if cooling else None, "master_pause": pause})
        return {"scope": "global", "captured_at": now.isoformat(), "open_incidents": incidents,
                "needs_owner": needs_owner, "running_clocks": clocks, "offline_nodes": offline_nodes,
                "quota_accounts": quota_accounts, "quota_incidents": quota_incidents,
                "autonomy": master_budget_state()}

    def create_flow_bug(run: dict, candidate: dict) -> dict | None:
        """Open a TESTFLOW Bug for a detected pattern, record it before any further Jira call, and move it to Intake."""
        run_lines = [f"- {item['id']} · {item['ticket']} · {item['step']} · node {item['node']} · release {item['release']}"
                     for item in candidate["runs"]]
        reason = ("at least three matching failed runs across at least two TESTFLOW tickets in 30 minutes"
                  if len(candidate["runs"]) >= 3 and len(candidate["tickets"]) >= 2 else
                  "a ticket_stuck incident for this error pattern remained open over 30 minutes")
        description = ("Automatically detected by the AI Flow master cycle.\n\n"
                       f"Fingerprint: {candidate['signature']}\n"
                       f"Pattern: {candidate['pattern']}\n"
                       f"Error kind: {candidate['error_kind']}\n"
                       f"Detection: {reason}.\n"
                       f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                       f"Steps: {', '.join(candidate['steps'])}\n"
                       f"Nodes: {', '.join(candidate['nodes'])}\n"
                       f"AI Flow releases: {', '.join(candidate['releases'])}\n"
                       "Related runs:\n" + "\n".join(run_lines) + "\n\n"
                       f"Suggested fix: {suggested_flow_fix(candidate)}")
        summary = f"[Flow bug] {candidate['pattern'].splitlines()[0]}"[:240]
        try:
            ticket_key = ctx.jira.create_issue("TESTFLOW", summary, description, issuetype="Bug",
                                               labels=["flow-bug"])
        except Exception as exc:  # Keep the monitoring snapshot available and retry detection next cycle.
            audit("jira", "flow_bug_create", "", str(run["id"]), run["fence"], run["account_id"], False,
                  "Jira issue creation failed", {"signature": candidate["signature"], "error": type(exc).__name__})
            log.warning("could not create flow-bug for signature %s: %s",
                        candidate["signature"], type(exc).__name__)
            return None
        # Record the new Bug before any further Jira call, so a failure below never leads to a second Bug for
        # this signature. The status is refreshed from Jira on the next cycle; it is not Intake until moved.
        run_ids = sorted(item["id"] for item in candidate["runs"])
        ctx.db.run("INSERT INTO flow_bug_tracker (signature, ticket_key, run_ids, repeat_count, in_intake)"
                   " VALUES (%s,%s,%s,0,false) ON CONFLICT (signature) DO UPDATE SET ticket_key=EXCLUDED.ticket_key,"
                   " run_ids=EXCLUDED.run_ids, repeat_count=0, in_intake=false, updated_at=now()",
                   (candidate["signature"], ticket_key, run_ids))
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,'TESTFLOW','Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (ticket_key, ["flow-bug"], summary, description))
        audit("jira", "flow_bug_create", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
              "created", {"reason": reason, "signature": candidate["signature"], "run_ids": run_ids,
                          "tickets": candidate["tickets"], "labels": ["flow-bug"]})
        status = move_flow_bug_to_intake(run, ticket_key, candidate["signature"])
        ctx.events.emit("flow_bug.created", ticket=ticket_key, project="TESTFLOW", run_id=str(run["id"]),
                        fence=run["fence"], account=run["account_id"], node=run["node"],
                        outcome=status or "created", reason=candidate["pattern"],
                        data={"signature": candidate["signature"], "run_ids": run_ids,
                              "tickets": candidate["tickets"]})
        return {"action": "created", "ticket": ticket_key, "signature": candidate["signature"],
                "run_ids": run_ids, "in_intake": status == "Intake"}

    def process_flow_bugs(run: dict) -> list[dict]:
        """Create or update detected TESTFLOW Bugs from this valid, fenced master cycle."""
        if not ctx.jira:
            return []
        reports: list[dict] = []
        terminal = {"done", "closed", "resolved", "cancelled", "canceled"}
        for candidate in flow_bug_candidates(ctx):
            tracker = ctx.db.one("SELECT ticket_key, run_ids, repeat_count, in_intake FROM flow_bug_tracker"
                                 " WHERE signature=%s", (candidate["signature"],))
            if tracker:
                ticket_key = tracker["ticket_key"]
                try:
                    status = ctx.jira.status(ticket_key)
                except Exception as exc:  # Jira reads can fail transiently; retry on the next master cycle.
                    log.warning("could not check status of flow-bug %s: %s", ticket_key, type(exc).__name__)
                    continue
                ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket_key))
                if status.casefold() not in terminal:
                    if not tracker["in_intake"]:  # created earlier, but the move to Intake failed: retry only that
                        if status == "Intake":
                            ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now()"
                                       " WHERE signature=%s", (candidate["signature"],))
                        elif move_flow_bug_to_intake(run, ticket_key, candidate["signature"]):
                            reports.append({"action": "moved_to_intake", "ticket": ticket_key,
                                            "signature": candidate["signature"]})
                    known = set(tracker["run_ids"] or [])
                    new_runs = [r for r in candidate["runs"] if r["id"] not in known]
                    if new_runs:
                        repeat_count = int(tracker["repeat_count"]) + len(new_runs)
                        total = len(known) + len(new_runs)
                        body = ("AI Flow · master · repeated flow error\n"
                                f"Pattern: {candidate['pattern']}\n"
                                f"Repeat count: {repeat_count} additional failed runs since this Bug was opened "
                                f"({total} matching runs recorded).\n"
                                f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                                "New run IDs: " + ", ".join(r["id"] for r in new_runs))
                        comment_id = ctx.jira.comment(ticket_key, body)
                        all_ids = sorted(known | {r["id"] for r in new_runs})
                        ctx.db.run("UPDATE flow_bug_tracker SET run_ids=%s, repeat_count=%s, updated_at=now()"
                                   " WHERE signature=%s", (all_ids, repeat_count, candidate["signature"]))
                        audit("jira", "flow_bug_repeat", ticket_key, str(run["id"]), run["fence"],
                              run["account_id"], True, comment_id,
                              {"signature": candidate["signature"], "repeat_count": repeat_count,
                               "new_run_ids": [r["id"] for r in new_runs]})
                        ctx.events.emit("flow_bug.repeated", ticket=ticket_key, project="TESTFLOW",
                                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"],
                                        node=run["node"], outcome="commented", reason=candidate["pattern"],
                                        data={"repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                        reports.append({"action": "repeated", "ticket": ticket_key,
                                        "repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                    continue

            if not candidate["creation_eligible"]:
                # The tracker keeps low-volume recurrences visible for open Bugs, but a closed Bug only
                # gets replaced after the normal repetition threshold or an aged ticket_stuck incident.
                continue

            created = create_flow_bug(run, candidate)
            if created:
                reports.append(created)
        return reports

    def move_flow_bug_to_intake(run: dict, ticket_key: str, signature: str) -> str | None:
        """Move a tracked flow-bug to Intake once; returns "Intake", or None when the move failed (retried later)."""
        try:
            status = ctx.jira.transition_to(ticket_key, "Intake")
            if status != "Intake":
                raise RuntimeError("flow-bug did not reach Intake")
        except Exception as exc:  # The tracker row keeps the key; the next master cycle retries only the move.
            audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], False,
                  "Intake transition failed", {"signature": signature, "error": type(exc).__name__})
            log.warning("could not move flow-bug %s to Intake: %s", ticket_key, type(exc).__name__)
            return None
        ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now() WHERE signature=%s", (signature,))
        ctx.db.run("UPDATE tickets SET status='Intake', synced_at=now() WHERE key=%s", (ticket_key,))
        audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
              "Intake", {"signature": signature})
        return "Intake"

    # --- nodes ----------------------------------------------------------------------------------------------

    def create_join_grant(name: str, role: str, project: str) -> tuple[str, datetime]:
        """Create the shared five-minute grant used by both Command Center entry points."""
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid node name")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
            raise HTTPException(422, "invalid node role")
        repo = ctx.repo()
        if project not in repo.projects:
            raise HTTPException(422, "unknown project")
        dynamic = repo.inventory_in_db or name not in repo.nodes
        if not dynamic and project not in repo.nodes[name].projects:
            raise HTTPException(422, "project is not assigned to this node in the control repo")
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                # The count, join grant, and pending node insert share the lock and transaction. A second request
                # waits here, then sees the first request's node before checking the last available slot.
                licensing.lock_resource_admission(conn)
                existing = conn.execute(
                    "SELECT name,removed_at,(token_hash<>'' AND last_heartbeat > now()-interval '75 seconds') AS live"
                    " FROM nodes WHERE name=%s FOR UPDATE", (name,)).fetchone()
                if existing and existing["live"] and not existing["removed_at"]:
                    raise HTTPException(409, "a live node already uses this name")
                if not existing or existing["removed_at"]:
                    count = int(conn.execute(
                        "SELECT count(*) AS count FROM nodes WHERE removed_at IS NULL").fetchone()["count"])
                    require_capacity("machines", count, connection=conn)
                token = secrets.token_urlsafe(32)
                issued = conn.execute("INSERT INTO node_join_tokens (token_hash,node_name,role,project,dynamic,expires_at)"
                                      " VALUES (%s,%s,%s,%s,%s,now()+interval '5 minutes')"
                                      " ON CONFLICT (node_name) WHERE used_at IS NULL DO UPDATE SET"
                                      " token_hash=EXCLUDED.token_hash,role=EXCLUDED.role,project=EXCLUDED.project,"
                                      " dynamic=EXCLUDED.dynamic,created_at=now(),expires_at=EXCLUDED.expires_at"
                                      " RETURNING expires_at",
                                      (h(token), name, role, project, dynamic)).fetchone()
                # Register a pending dynamic node so Command Center can show it before its first heartbeat.
                conn.execute("INSERT INTO nodes (name,role,status,projects,dynamic,accepting_work,removed_at)"
                             " VALUES (%s,%s,'joined',%s,%s,true,NULL) ON CONFLICT (name) DO UPDATE"
                             " SET removed_at=NULL,accepting_work=true WHERE nodes.removed_at IS NOT NULL",
                             (name, role, [project], dynamic))
        return token, issued["expires_at"]
    return SimpleNamespace(bot_identity=bot_identity, node_auth=node_auth, release_reader=release_reader, reviewer_auth=reviewer_auth, owner_auth=owner_auth, deploy_auth=deploy_auth, audit=audit, audit_in=audit_in, console_user=console_user, optional_console_user=optional_console_user, require_capacity=require_capacity, run_row=run_row, fenced=fenced, master_run=master_run, reviewer_run=reviewer_run, finish_reviewer_run=finish_reviewer_run, reviewer_comment=reviewer_comment, reviewer_transition=reviewer_transition, reviewer_labels=reviewer_labels, record_reviewer_action=record_reviewer_action, repository_for_run=repository_for_run, settings_for_run=settings_for_run, matches_run_repository=matches_run_repository, merge_command=merge_command, queued_merge=queued_merge, master_budget_state=master_budget_state, account_master_paused=account_master_paused, mark_machine_auth_failure=mark_machine_auth_failure, quota_forecast_at_risk=quota_forecast_at_risk, refresh_quota_forecast=refresh_quota_forecast, quota_peer_accounts=quota_peer_accounts, account_quota_at_risk=account_quota_at_risk, quota_peer_release_candidates=quota_peer_release_candidates, master_snapshot=master_snapshot, process_flow_bugs=process_flow_bugs, move_flow_bug_to_intake=move_flow_bug_to_intake, create_join_grant=create_join_grant)
