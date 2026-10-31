"""Routes of the business feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import copy
import threading
import time
from types import SimpleNamespace

from ...state_schema import STATE_RESPONSE_SCHEMA

from .bodies import ChangeRequestBody, StakeholderVerdictBody
from typing import Any
from fastapi import Depends, Header, HTTPException, Query, Request
from ... import (business)
from ...console_auth import (has_permission)
from ...stakeholder_screenshot import decode_screenshot


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import github_gateway  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    state_snapshot_lock = threading.Lock()
    state_snapshot_cache: dict[bool, tuple[float, dict[str, Any]]] = {}

    def cached_state_snapshot(include_signin: bool) -> dict[str, Any]:
        """Coalesce identical console snapshot reads while several views load at the same time."""
        requested_at = time.monotonic()
        with state_snapshot_lock:
            cached = state_snapshot_cache.get(include_signin)
            # A caller that began while the preceding snapshot was being built may share it.
            # Requests arriving after that read finished always see current database state.
            if cached and requested_at < cached[0]:
                return cached[1]
            from ...state import snapshot
            data = snapshot(ctx, include_signin=include_signin)
            state_snapshot_cache[include_signin] = (time.monotonic(), data)
            return data
    deploy_auth = deps.deploy_auth
    optional_console_user = deps.optional_console_user
    owner_auth = deps.owner_auth
    # --- Business view: change requests (same access as the console, no node token) -----------------------------
    def business_project(key: str) -> str:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        return key

    @app.get("/v1/business/{key}/change-requests")
    def business_change_requests(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "jira_site": ctx.settings.jira_site, "epics": business.epics(ctx.db, project),
                "change_requests": business.change_requests(ctx.db, project, ctx.settings.jira_site)}

    @app.get("/v1/business/{key}/release-readiness")
    def business_release_readiness(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_readiness(ctx.db, project)}

    @app.get("/v1/business/{key}/release-progress")
    def business_release_progress(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_progress_of(ctx.db, project)}

    @app.post("/v1/business/{key}/change-requests")
    def business_change_request(key: str, body: ChangeRequestBody) -> dict:
        project = business_project(key)
        try:
            title, description, epic = business.validate(ctx.db, project, body.title, body.description, body.epic)
        except business.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        created = business.create(ctx, project, title, description, epic)
        audit("jira", "create", created["key"], "", None, business.ACTOR, True, created["status"],
              {"project": project, "type": "Story", "labels": [business.LABEL], "epic": epic,
               "chars": len(description)})
        ctx.events.emit("business.change_request", ticket=created["key"], project=project, outcome=created["status"],
                        reason=title[:200])
        return created

    # --- Stakeholder acceptance and UAT feedback --------------------------------------------------------------
    def stakeholder_project(key: str):
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, f"unknown project {key}")
        return project

    def stakeholder_changes(project: str) -> list[dict[str, Any]]:
        rows = ctx.db.all(
            "SELECT DISTINCT ON (t.key) t.key, t.summary, t.type, t.status, t.parent_key, p.type AS parent_type,"
            " r.branch, r.head_sha, r.summary AS change_summary, r.details, r.started_at, r.ended_at"
            " FROM runs r JOIN tickets t ON t.key=r.ticket_key LEFT JOIN tickets p ON p.key=t.parent_key"
            " WHERE r.project=%s AND t.project=%s AND r.step='dev' AND r.status<>'running'"
            " AND r.outcome IN ('done','merged')"
            " ORDER BY t.key, r.started_at DESC", (project, project))
        rows.sort(key=lambda r: r["ended_at"] or r["started_at"], reverse=True)
        out = []
        for row in rows[:30]:
            details = row["details"] or {}
            epic = row["key"] if row["type"] == "Epic" else (
                row["parent_key"] if row["parent_type"] == "Epic" else "")
            release_commit = (details.get("release") or details.get("version") or row["head_sha"] or row["branch"] or "")
            out.append({"key": row["key"], "summary": row["summary"], "type": row["type"],
                        "status": row["status"], "epic": epic, "branch": row["branch"],
                        "commit": row["head_sha"], "release_commit": release_commit,
                        "change_summary": row["change_summary"],
                        "changed_at": (row["ended_at"] or row["started_at"]).isoformat()})
        return out

    @app.get("/v1/stakeholder/{key}")
    def stakeholder_view(key: str) -> dict:
        project = stakeholder_project(key)
        configured = project.urls or {}
        return {"project": key, "urls": {"develop": str(configured.get("develop") or ""),
                                          "production": str(configured.get("production") or "")},
                "changes": stakeholder_changes(key)}

    @app.post("/v1/stakeholder/{key}/verdict")
    def stakeholder_verdict(key: str, body: StakeholderVerdictBody) -> dict:
        project = stakeholder_project(key)
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        sender = body.sender.strip()
        if not sender:
            raise HTTPException(400, "sender is required")
        change = ctx.db.one("SELECT key, summary, type, parent_key FROM tickets WHERE key=%s AND project=%s",
                            (body.change_key, key))
        if not change:
            raise HTTPException(400, "change must belong to this project")
        target = body.target_key.strip() or body.change_key
        if target != body.change_key and target != change["parent_key"]:
            raise HTTPException(400, "acceptance target must be the related ticket or its epic")
        if target != body.change_key and not ctx.db.one(
                "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'", (target, key)):
            raise HTTPException(400, "acceptance target must be the related epic")
        if body.verdict == "not_expected" and (not body.expected.strip() or not body.actual.strip()):
            raise HTTPException(400, "expected and actual behaviour are required for UAT feedback")

        screenshot = decode_screenshot(body.screenshot_data, body.screenshot_filename)

        page_url = str((project.urls or {}).get(body.environment) or "").strip()
        page_label = page_url or "chưa deploy"
        release_commit = body.release_commit.strip() or "chưa có dữ liệu"
        if body.verdict == "expected":
            comment = (f"Comment nghiệm thu\nMôi trường: {body.environment}\n"
                       f"Release hoặc commit: {release_commit}\nNgười gửi: {sender}")
            try:
                comment_id = ctx.jira.comment(target, comment)
            except Exception as exc:  # Jira adapters and HTTP clients can raise different transport exceptions.
                log.warning("Stakeholder acceptance comment failed for %s", target)
                raise HTTPException(502, "Jira could not record the acceptance") from exc
            audit("jira", "stakeholder_acceptance", target, "", None, sender, True, comment_id,
                  {"project": key, "change": body.change_key, "environment": body.environment})
            ctx.events.emit("stakeholder.acceptance", ticket=target, project=key, outcome="accepted",
                            account=sender, reason=body.environment)
            return {"ok": True, "verdict": body.verdict, "ticket": target, "comment_id": comment_id}

        description = (f"Related change: {change['key']} - {change['summary']}\n"
                       f"Page URL: {page_label}\nEnvironment: {body.environment}\n"
                       f"Release or commit: {release_commit}\n"
                       f"Expected behaviour (mong đợi):\n{body.expected.strip()}\n\n"
                       f"Actual behaviour (thực tế):\n{body.actual.strip()}\n\n"
                       f"Sender (người gửi): {sender}")
        summary = f"[UAT {body.environment}] {change['key']}: {body.actual.strip().splitlines()[0][:120]}"
        try:
            issue_key = ctx.jira.create_issue(key, summary, description, issuetype="Bug", labels=["uat-bug"])
            status = ctx.jira.transition_to(issue_key, "Intake")
            if status != "Intake":
                raise RuntimeError("new UAT issue did not reach Intake")
            if screenshot:
                ctx.jira.add_attachment(issue_key, screenshot[0], screenshot[1], screenshot[2])
        except Exception as exc:  # Never tell the stakeholder a partial Jira write succeeded.
            log.warning("Stakeholder UAT issue creation failed for project %s", key)
            raise HTTPException(502, "Jira could not record the UAT feedback") from exc
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (issue_key, key, ["uat-bug"], summary, description))
        audit("jira", "stakeholder_bug", issue_key, "", None, sender, True, status,
              {"project": key, "change": body.change_key, "environment": body.environment,
               "screenshot": bool(screenshot)})
        ctx.events.emit("stakeholder.feedback", ticket=issue_key, project=key, outcome="created",
                        account=sender, reason=body.environment, data={"related": body.change_key})
        return {"ok": True, "verdict": body.verdict, "key": issue_key, "status": status}

    @app.get("/v1/state", responses={200: {"description": "Command Center, Delivery, and SA snapshot",
                                            "content": {"application/json": {"schema": STATE_RESPONSE_SCHEMA}}}})
    def state(request: Request, authorization: str = Header(default="")) -> dict:
        if authorization:  # the deploy scripts on the control machine read it with the owner token (login is on)
            owner_auth(request, authorization)
        # The login middleware authenticates console sessions and permits this legacy owner-token route;
        # resolving the cookie here keeps scoped session data filtered while allowing the owner token alone.
        user = optional_console_user(request)
        include_signin = bool(user and has_permission(user.effective_roles, "nodes.manage"))
        data = cached_state_snapshot(include_signin)
        all_tickets = data["tickets"]
        if user and user.project_keys is not None:
            data = copy.deepcopy(data)
            all_tickets = data["tickets"]
            visible = set(user.project_keys)
            data["projects"] = [project for project in data["projects"] if project["key"] in visible]
            data["tickets"] = [ticket for ticket in all_tickets if ticket["project"] in visible]
            ticket_keys = {ticket["key"] for ticket in data["tickets"]}
            data["active_runs"] = [run for run in data["active_runs"] if run.get("project") in visible]
            data["recent_runs"] = [run for run in data["recent_runs"] if run.get("ticket") in ticket_keys]
            data["events"] = [event for event in data["events"] if event.get("ticket") in ticket_keys]
            # Project level artifacts carry different shapes across releases; omit aggregates for restricted users.
            data["release_readiness"] = []
            data["release_progress"] = []
            data["rollout"] = {}
            data["totals"] = ctx.db.one(
                "SELECT count(*) FILTER (WHERE started_at > now()-interval '24 hours') AS runs_24h,"
                " coalesce(sum(cost_usd) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS cost_24h,"
                " coalesce(sum(tokens_in) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_in_24h,"
                " coalesce(sum(tokens_out) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_out_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome='done') AS done_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome = ANY(%s)) AS failed_24h"
                " FROM runs WHERE project=ANY(%s)",
                (["failed", "blocked", "timeout", "lost"], list(visible)))
            data["totals"] = {key: int(value) if key in {"runs_24h", "tokens_in_24h", "tokens_out_24h",
                                                               "done_24h", "failed_24h"} else float(value)
                              for key, value in data["totals"].items()}
            visible_nodes = {node["name"] for node in data["nodes"] if visible.intersection(node.get("projects") or [])}
            data["nodes"] = [node for node in data["nodes"] if node["name"] in visible_nodes]
            data["accounts"] = [account for account in data["accounts"]
                                if any(machine.get("node") in visible_nodes
                                       for machine in account.get("machines") or [])]
            data["incidents"] = [item for item in data["incidents"]
                                 if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["incidents_recent"] = [item for item in data["incidents_recent"]
                                         if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["sa"] = {}
            data["version_policy"] = {}
        if user and not has_permission(user.effective_roles, "view.machines_ai"):
            data["nodes"] = []
            data["accounts"] = []
            data["sa"] = {}
            data["role_health"] = {"roles": [], "settings": {}}
            data["version_policy"] = {}
            data["incidents"] = []
            data["incidents_recent"] = []
            data["events"] = [{key: event.get(key) for key in ("ts", "type", "ticket", "outcome", "reason")}
                               for event in data["events"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "already_done", "ended_age_s")}
            data["active_runs"] = [{key: run[key] for key in ("id", "ticket", "project", "summary", "step",
                                                                  "elapsed_s", "heartbeat_age_s", "lease_ms", "progress")}
                                    for run in data["active_runs"]]
            data["recent_runs"] = [{key: run[key] for key in ("id", "ticket", "summary", "step", "outcome",
                                                                  "error_kind", "duration_s", "pr_url", "already_done",
                                                                  "ended_age_s")}
                                    for run in data["recent_runs"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "already_done", "ended_age_s")}
        return data

    @app.get("/v1/deploy/state")
    def deploy_state(_deploy: str = Depends(deploy_auth)) -> dict:
        """The registry state for aiflow4-autodeploy; works with console sign-in on because it checks the token."""
        from ...state import snapshot
        return snapshot(ctx)

    @app.get("/v1/deploy/ci-state")
    def deploy_ci_state(repo: str = Query(pattern=r"^[\w.-]+/[\w.-]+$"), sha: str = Query(pattern=r"^[0-9a-fA-F]{7,64}$"),
                        _deploy: str = Depends(deploy_auth)) -> dict:
        """CI state of a commit (pending|failed|ok|none) through the connection for its repository."""
        from ...canary import ci_state
        gateway = github_gateway(ctx, repo)
        return {"repo": repo, "sha": sha, "state": ci_state(gateway, repo, sha)}

    return SimpleNamespace(business_project=business_project, business_change_requests=business_change_requests, business_release_readiness=business_release_readiness, business_release_progress=business_release_progress, business_change_request=business_change_request, stakeholder_project=stakeholder_project, stakeholder_changes=stakeholder_changes, stakeholder_view=stakeholder_view, stakeholder_verdict=stakeholder_verdict, state=state, deploy_state=deploy_state, deploy_ci_state=deploy_ci_state)
