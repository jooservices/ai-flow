"""Routes of the github_fenced feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import hashlib
import hmac
import csv
import io
import re
import json
import httpx
from datetime import timedelta
from types import SimpleNamespace

from .bodies import BranchCleanupBody, GitHubTokenBody, MergeBody, PRBody, ReviewerBody
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import (PlainTextResponse, Response)
from ... import (escalations,
               version_release)
from ... import merge_queue, review_queue_api
from ... import merge_flow
from ...route_deps import github_connection_used
from ...github import GitHubError
from ... import pr_comments
from ...pr_comments import PRCommentBody
from ...features.work.constants import utcnow


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import _git_proxy_credential, _issue_git_proxy_token, _receive_pack_target, _verify_git_proxy_token, github_gateway, github_gateway_for_connection, log  # noqa: PLC0415 - api imports this module

    def _github_git_upstream(*args, **kwargs):
        from ... import api as _api  # noqa: PLC0415 - looked up when called, so tests may patch it
        return _api._github_git_upstream(*args, **kwargs)

    audit = deps.audit
    console_user = deps.console_user
    fenced = deps.fenced
    finish_reviewer_run = deps.finish_reviewer_run
    fleet_routes = deps.fleet_routes
    matches_run_repository = deps.matches_run_repository
    merge_command = deps.merge_command
    node_auth = deps.node_auth
    optional_console_user = deps.optional_console_user
    projects_routes = deps.projects_routes
    queued_merge = deps.queued_merge
    record_reviewer_action = deps.record_reviewer_action
    repository_for_run = deps.repository_for_run
    reviewer_auth = deps.reviewer_auth
    reviewer_comment = deps.reviewer_comment
    reviewer_labels = deps.reviewer_labels
    reviewer_run = deps.reviewer_run
    reviewer_transition = deps.reviewer_transition
    run_row = deps.run_row
    settings_for_run = deps.settings_for_run
    # --- GitHub (fenced) --------------------------------------------------------------------------------------
    @app.post("/v1/github/token")
    def github_token(body: GitHubTokenBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        permissions = ({"contents": "read", "metadata": "read"}
                       if run["step"] in ("triage", "version_release", "post_merge") else
                       {"contents": "write", "metadata": "read", "workflows": "write"})
        if run["step"] == "version_release" and not version_release.enabled(project):
            raise HTTPException(403, "project is not enabled for version releases")
        github = github_gateway(ctx, repository)
        if getattr(github, "kind", "app") == "token":
            branch = body.branch.strip()
            if branch and (run["step"] != "dev" or not branch.startswith(f"{run['ticket_key']}-")
                           or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                           or branch.startswith("/") or branch.endswith("/") or ".." in branch
                           or "@{" in branch):
                raise HTTPException(422, "the Git transfer branch is invalid for this run")
            expires = utcnow() + timedelta(hours=1)
            connection_id = github_connection_used.get()
            capability = _issue_git_proxy_token(ctx, {
                "connection": connection_id, "repo": repository.lower(), "run_id": body.run_id,
                "fence": body.fence, "node": node, "branch": branch,
                "expires": int(expires.timestamp()),
            })
            audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
                  "repository Git relay capability issued",
                  {"repo": repository, "branch": branch, "expires": expires.isoformat(), "transport": "control"})
            return {"token": capability, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{repository}.git"}
        token, expires = github.token(repository.split("/", 1)[-1], permissions)
        audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "issued",
              {"repo": repository, "expires": expires.isoformat()})
        return {"token": token, "expires_at": expires.isoformat()}

    async def github_git_proxy(git_path: str, request: Request) -> Response:
        """Broker token-backed Git smart HTTP without exposing the stored access token to a worker."""
        capability = _verify_git_proxy_token(ctx, _git_proxy_credential(request))
        if not capability:
            raise HTTPException(401, "Git repository transfer credential is invalid or expired",
                                headers={"WWW-Authenticate": 'Basic realm="AI Flow Git transfer"'})
        repository, separator, suffix = git_path.partition(".git")
        if (not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or repository.lower() != str(capability.get("repo", ""))):
            raise HTTPException(403, "Git repository transfer is outside this credential's scope")
        suffix = ".git" + suffix
        if request.method == "GET" and suffix == ".git/info/refs":
            service = request.query_params.get("service", "")
            if service not in ("git-upload-pack", "git-receive-pack"):
                raise HTTPException(400, "unsupported Git service")
            operation = service
        elif request.method == "POST" and suffix in (".git/git-upload-pack", ".git/git-receive-pack"):
            operation = suffix.rsplit("/", 1)[-1]
        else:
            raise HTTPException(404, "unsupported Git repository transfer operation")

        purpose = str(capability.get("purpose") or "run")
        run = None
        run_id = ""
        fence = -1
        if purpose == "check_probe":
            if operation != "git-upload-pack":
                raise HTTPException(403, "check-probe Git credentials are read-only")
            node_name = str(capability.get("node") or "")
            project_key = str(capability.get("project") or "")
            project = fleet_routes.probe_project(node_name, project_key)
            if repository.lower() not in {name.lower() for name in project.configured_repositories}:
                raise HTTPException(409, "check-probe repository is no longer configured")
        elif purpose == "run":
            run_id = str(capability.get("run_id") or "")
            run = run_row(run_id)
            fence = int(capability.get("fence", -1))
            if (run["node"] != capability.get("node") or int(run["fence"]) != fence
                    or not ctx.leases.valid(run["ticket_key"], run_id, fence)
                    or not matches_run_repository(repository_for_run(run, ctx.repo().projects.get(run["project"])),
                                                  repository)):
                raise HTTPException(409, "Git repository transfer run is no longer active")
            if operation == "git-receive-pack" and not capability.get("branch"):
                raise HTTPException(403, "Git push is not allowed by this read-only credential")
        else:
            raise HTTPException(403, "Git repository transfer credential has an unsupported purpose")
        body_bytes = await request.body() if request.method == "POST" else b""
        branch = str(capability.get("branch") or "")
        if request.method == "POST" and operation == "git-receive-pack":
            targets = _receive_pack_target(body_bytes)
            if targets != {f"refs/heads/{branch}"}:
                raise HTTPException(403, "Git push is limited to the branch assigned to this run")

        connection_id = str(capability.get("connection") or "")
        gateway = github_gateway_for_connection(ctx, connection_id, repository)
        if getattr(gateway, "kind", "app") != "token":
            raise HTTPException(403, "Git relay credentials are only valid for access-token connections")
        upstream_token, _ = gateway.token(repository.split("/", 1)[-1])
        forwarded_headers = {key: value for key, value in request.headers.items()
                             if key.lower() in {"accept", "content-type", "git-protocol"}}
        try:
            import anyio
            upstream = await anyio.to_thread.run_sync(
                _github_git_upstream, repository, suffix, request.method,
                list(request.query_params.multi_items()), forwarded_headers, body_bytes, upstream_token)
        except httpx.HTTPError as exc:
            log.warning("GitHub repository transfer failed (%s)", type(exc).__name__)
            raise HTTPException(502, "GitHub repository transfer failed") from exc
        if request.method == "POST" and operation == "git-receive-pack":
            if run is None:
                raise HTTPException(403, "check-probe Git credentials are read-only")
            accepted = 200 <= upstream.status_code < 300
            audit("github", "git-push", run["ticket_key"], run_id, fence, run["account_id"], accepted,
                  "pushed" if accepted else f"GitHub returned {upstream.status_code}",
                  {"repo": repository, "branch": branch})
        response_headers = {key: value for key, value in upstream.headers.items()
                            if key.lower() in {"content-type", "cache-control", "expires", "pragma"}}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    @app.get("/v1/github/git/{git_path:path}", operation_id="githubGitProxyGet")
    async def github_git_proxy_get(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.post("/v1/github/git/{git_path:path}", operation_id="githubGitProxyPost")
    async def github_git_proxy_post(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.get("/v1/github/pr")
    def github_find_pr(run_id: str = Query(min_length=1), fence: int = Query(ge=1),
                       repo: str = Query(min_length=1, max_length=200),
                       head: str = Query(min_length=1, max_length=240), node: str = Depends(node_auth)) -> dict:
        run = fenced(run_id, fence, "github", "find_pr", {"repo": repo, "head": head})
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        pr = github_gateway(ctx, repository).find_pr(repository.split("/", 1)[-1], head)
        found = {"found": bool(pr)}
        if pr:
            found.update(url=pr.get("html_url", ""), number=pr.get("number"))
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            if head_sha:
                found["head_sha"] = head_sha
        audit("github", "find_pr", run["ticket_key"], run_id, fence, run["account_id"], True,
              found.get("url", "not found"), {"repo": repository, "head": head})
        return found

    @app.post("/v1/github/pr")
    def github_pr(body: PRBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "pr", {"repo": body.repo, "head": body.head})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        github = github_gateway(ctx, repository)
        settings = settings_for_run(run, project)
        if body.base != settings["base"]:
            raise HTTPException(403, "pull request base does not match the repository settings")
        pr = github.create_pr(repository.split("/", 1)[-1], body.head, body.base, body.title, body.body, body.draft)
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s WHERE id=%s", (pr["html_url"], body.head, body.run_id))
        audit("github", "pr", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, pr["html_url"],
              {"number": pr.get("number"), "draft": body.draft, "repo": repository})
        ctx.events.emit("github.pr", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="opened", pr_url=pr["html_url"])
        return {"url": pr["html_url"], "number": pr.get("number")}

    @app.post("/v1/github/merge")
    def github_merge(body: MergeBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "merge", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run can merge")
        # ready pull requests merge one after another, each brought up to date first (docs/v4/merge-conflicts.md)
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        res = merge_queue.merge_next(ctx.db, github_gateway(ctx, repository), repository, body.number,
                                     run["ticket_key"], str(run["id"]),
                                     lambda: merge_command(run, body.number, body.title, body.method, node))
        if res.get("updated") or res.get("conflict"):
            ctx.events.emit("github.merge_queue", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                            account=run["account_id"], node=node,
                            outcome="updated" if res.get("updated") else "conflict", reason=res.get("message", "")[:200])
        if res.get("merged"):  # the verify step's merge: the commit and the time are kept on the ticket
            merge_flow.record_merge(ctx, run["ticket_key"], merge_flow.latest_pr(ctx.db, run["ticket_key"]),
                                    str(res.get("sha") or ""), "flow", settled=False)
        return res

    review_queue = review_queue_api.ReviewQueueAPI(
        ctx, audit, repository_for_run, github_gateway, lambda t: projects_routes.ticket_epic(t), log)
    review_item_kind = review_queue.item_kind
    current_review_item_kind = review_queue.current_item_kind
    review_thread = review_queue.thread
    review_data = review_queue.data
    app.state.review_queue = review_queue  # routers outside api.py reach the queue through the app

    @app.post("/v1/github/webhook", include_in_schema=False)
    async def github_webhook(request: Request) -> dict:
        """Optional: a pull request event makes the reconcile loop look at that pull request at once. Without a
        configured secret the route does not exist for callers; the one-minute loop works alone."""
        secret = ctx.settings.github_webhook_secret
        if not secret:
            raise HTTPException(404, "GitHub webhooks are not configured")
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(401, "invalid webhook signature")
        if request.headers.get("X-GitHub-Event") != "pull_request":
            return {"reconciled": 0}
        try:
            payload = json.loads(body)
            target = (str(payload["repository"]["full_name"]), int(payload["pull_request"]["number"]))
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "not a pull request event") from None
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(merge_flow.reconcile, ctx, target)
        await run_in_threadpool(merge_flow.merge_ready, ctx)
        return {"reconciled": result["checked"]}

    review_queue.mount_human_reviews(app, console_user, optional_console_user)

    @app.get("/v1/reviews/export.csv")
    def export_review_metrics(request: Request, project: str = Query(default="")) -> PlainTextResponse:
        user = (console_user(request, "view.human_review") if ctx.settings.console_login
                else optional_console_user(request))
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            audit("console", "access_denied", "", "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "project": project,
                                                    "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        metrics = review_data(project, scope)["metrics"]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("metric", "value", "project"))
        for name, value in (
            ("people_needed_this_week", metrics["people_needed_this_week"]),
            ("people_needed_eight_weeks_ago", metrics["people_needed_eight_weeks_ago"]),
            ("finished_with_no_person_pct", metrics["finished_with_no_person_pct"]),
            ("median_wait_minutes", metrics["median_wait_minutes"]),
            ("approved_first_time_pct", metrics["approved_first_time_pct"]),
            ("sent_back_or_rejected_pct", metrics["sent_back_or_rejected_pct"]),
        ):
            writer.writerow((name, "" if value is None else value, project or "all"))
        writer.writerow(())
        writer.writerow(("week", "reason", "count", "project"))
        for week in metrics["weekly"]:
            for reason, count in week["by_reason"].items():
                writer.writerow((week["week"], reason, count, project or "all"))
        return PlainTextResponse(output.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=human-review-metrics.csv"})

    @app.post("/v1/reviews/{key}/approve")
    def approve_review(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to approve")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            ctx.db.run("UPDATE tickets SET approved_at=coalesce(approved_at, now()) WHERE key=%s", (key,))
            result = queued_merge(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                # approved: the merge step of the flow takes over, so nobody has to press merge later
                note = (f"Human review approval could not merge PR #{number} yet: {result.get('message', 'merge refused')}."
                        " The flow keeps the ticket as Ready to merge and merges it as soon as the pull request can merge.")
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                queued = ""
                try:
                    queued = reviewer_transition(key, run_id, fence, merge_flow.READY_TO_MERGE, "approved, merge pending")
                except Exception as e:  # noqa: BLE001 - the Jira workflow may lack the status; approval still queues it
                    log.info("%s stays in Human review until it merges: %s", key, e)
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "queued": True, "status": queued or ticket["status"],
                        "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review approved in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            merge_flow.record_merge(ctx, key, merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow",
                                    settled=False)
            status = reviewer_transition(key, run_id, fence, "Done", "human review approved")
            record_reviewer_action(ticket, run_id, fence, "approve")
            finish_reviewer_run(key, run_id, fence, "merged", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review approval failed for %s", key)
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise HTTPException(502, "could not complete the review approval") from e

    @app.post("/v1/reviews/{key}/rework")
    def request_review_rework(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        comment = body.comment.strip()
        if not comment:
            raise HTTPException(400, "a rework comment is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            note = f"Human review requested rework:\n\n{comment}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, add=("rework",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human review requested rework")
            record_reviewer_action(ticket, run_id, fence, "rework", comment)
            finish_reviewer_run(key, run_id, fence, "rework", comment)
            return {"status": status}
        except HTTPException:
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rework failed for %s", key)
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise HTTPException(502, "could not return the ticket for rework") from e

    @app.post("/v1/reviews/{key}/reject")
    def reject_review(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(400, "a rejection reason is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to reject")
            note = f"Human review rejected this change.\n\nReason: {reason}"
            reviewer_comment(key, run_id, fence, number, note)
            run = fenced(run_id, fence, "github", "close", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            repository = repository_for_run(run, project)
            gateway = github_gateway(ctx, repository)
            # a rejection is final: reconcile must not send the rejected ticket back because its PR is closed
            ctx.db.run("UPDATE tickets SET pr_state='closed', pr_url=coalesce(nullif(%s,''),pr_url),"
                       " merge_note=%s, pr_checked_at=now() WHERE key=%s",
                       ((merge_flow.latest_pr(ctx.db, key) or {}).get("url", ""), f"rejected in human review: {reason}"[:500], key))
            closed = gateway.close_pr(repository.split("/", 1)[-1], number)
            audit("github", "close", key, run_id, fence, run["account_id"], True,
                  "pull request closed", {"number": number, "repo": repository})
            reviewer_labels(key, run_id, fence, add=("rejected",), remove=("require-human",))
            status = reviewer_transition(key, run_id, fence, "Done", "human review rejected")
            record_reviewer_action(ticket, run_id, fence, "reject", reason)
            finish_reviewer_run(key, run_id, fence, "rejected", reason)
            return {"status": status, "closed": bool(closed)}
        except HTTPException:
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rejection failed for %s", key)
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise HTTPException(502, "could not reject the reviewed change") from e

    @app.post("/v1/reviews/{key}/ask-ai")
    def ask_review_ai(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        question = (body.question or body.comment).strip()
        if not question:
            raise HTTPException(400, "a question for the AI is required")
        ticket, run_id, fence, number = reviewer_run(key, ("Human review", "Needs owner", "To Do", "In Progress", "In Verify"))
        try:
            note = f"Human review asked the AI:\n\n{question}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = ticket["status"]
            if status != "Needs owner":
                status = reviewer_transition(key, run_id, fence, "Needs owner", "reviewer asked the AI")
            escalations.raise_reviewer_question(ctx.db, key, ticket["project"], run_id, question)
            record_reviewer_action(ticket, run_id, fence, "ask-ai", question)
            finish_reviewer_run(key, run_id, fence, "question", question)
            return {"status": status, "thread": review_thread(key)}
        except HTTPException:
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review AI question failed for %s", key)
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise HTTPException(502, "could not send the question to the AI") from e

    @app.post("/v1/reviews/{key}/answer")
    def answer_review_question(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        answer = (body.answer or body.comment).strip()
        if not answer:
            raise HTTPException(400, "an answer is required")
        ticket, run_id, fence, number = reviewer_run(
            key, ("Needs owner",), allowed_kinds=("question_from_ai", "decision_needed"))
        try:
            reviewer_comment(key, run_id, fence, number, f"Human answer to the AI:\n\n{answer}")
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human answered the AI question")
            record_reviewer_action(ticket, run_id, fence, "answer", answer)
            finish_reviewer_run(key, run_id, fence, "answered", answer)
            return {"status": status}
        except HTTPException:
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review answer failed for %s", key)
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise HTTPException(502, "could not record the answer") from e

    @app.post("/v1/reviews/{key}/skip-gate")
    def skip_review_gate(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if "require-human" not in (ticket.get("labels") or []):
                raise HTTPException(409, "this ticket has no human review gate to skip")
            if not number:
                raise HTTPException(409, "ticket has no pull request to skip the gate for")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = queued_merge(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                note = f"Human review gate could not be skipped because PR #{number} did not merge: "
                note += result.get("message", "merge refused")
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review gate skipped in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("require-human",))
            merge_flow.record_merge(ctx, key, merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow",
                                    settled=False)
            status = reviewer_transition(key, run_id, fence, "Done", "human review gate skipped")
            record_reviewer_action(ticket, run_id, fence, "skip-gate")
            finish_reviewer_run(key, run_id, fence, "skipped_gate", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review gate skip failed for %s", key)
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise HTTPException(502, "could not skip the human review gate") from e

    @app.post("/v1/github/update-branch")
    def github_update_branch(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "update-branch", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run updates a PR branch")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        res = github.update_branch(repository.split("/", 1)[-1], body.number)
        audit("github", "update-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], res["ok"],
              res["message"][:200], {"number": body.number, "repo": repository})
        return res

    @app.get("/v1/github/checks")
    def github_checks(run_id: str, fence: int, sha: str, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node or not ctx.leases.valid(run["ticket_key"], run_id, fence):
            raise HTTPException(409, "not the owner of this run")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        return {"checks": github.checks(repository.split("/", 1)[-1], sha)}

    @app.post("/v1/github/comment")
    def github_comment(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "comment", {"number": body.number})
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        outcome = pr_comments.post(github, repository.split("/", 1)[-1], body.number, body.kind, body.body)
        audit("github", "comment", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "ok",
              {"number": body.number, "repo": repository, "kind": body.kind, "outcome": outcome})
        return {"ok": True, "outcome": outcome}

    @app.post("/v1/github/delete-branch")
    def github_delete_branch(body: BranchCleanupBody, node: str = Depends(node_auth)) -> dict:
        """Delete the ticket's remote work branch through the connection assigned to its repository."""
        run = fenced(body.run_id, body.fence, "github", "delete-branch", {"branch": body.branch})
        if run["step"] not in ("dev", "verify"):
            raise HTTPException(403, "only a development or verify run cleans up a branch")
        branch = body.branch.strip()
        if (not branch.startswith(f"{run['ticket_key']}-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                or branch.endswith("/") or ".." in branch):
            raise HTTPException(422, "the branch does not belong to this ticket")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        try:
            deleted = github.delete_branch(repository.split("/", 1)[-1], branch)
        except GitHubError as exc:
            audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], False,
                  "GitHub refused the branch deletion", {"branch": branch, "repo": repository})
            raise HTTPException(502, "GitHub refused the branch deletion") from exc
        audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              "deleted" if deleted else "already gone", {"branch": branch, "repo": repository})
        return {"ok": True, "deleted": deleted}

    return SimpleNamespace(github_token=github_token, github_find_pr=github_find_pr, github_pr=github_pr, github_merge=github_merge, review_queue=review_queue, review_item_kind=review_item_kind, current_review_item_kind=current_review_item_kind, review_thread=review_thread, review_data=review_data, export_review_metrics=export_review_metrics, approve_review=approve_review, request_review_rework=request_review_rework, reject_review=reject_review, ask_review_ai=ask_review_ai, answer_review_question=answer_review_question, skip_review_gate=skip_review_gate, github_update_branch=github_update_branch, github_checks=github_checks, github_comment=github_comment, github_delete_branch=github_delete_branch)
