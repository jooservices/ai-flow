"""Human-review routes for fenced GitHub changes."""
from types import SimpleNamespace

from . import routes as _routes
from aiflow.v4.control.features.tickets import ticket_repository_of


def install_review_queue_routes(app, ctx, deps, runtime) -> SimpleNamespace:
    review_queue = _routes.review_queue_api.ReviewQueueAPI(
        ctx, deps.audit, deps.repository_for_run, runtime.github_gateway,
        lambda ticket: deps.projects_routes.ticket_epic(ticket), runtime.log)
    review_item_kind = review_queue.item_kind
    current_review_item_kind = review_queue.current_item_kind
    review_thread = review_queue.thread
    review_data = review_queue.data
    app.state.review_queue = review_queue  # routers outside api.py reach the queue through the app

    @app.post("/v1/github/webhook", include_in_schema=False)
    async def github_webhook(request: _routes.Request) -> dict:
        """Optional: a pull request event makes the reconcile loop look at that pull request at once. Without a
        configured secret the route does not exist for callers; the one-minute loop works alone."""
        secret = ctx.settings.github_webhook_secret
        if not secret:
            raise _routes.HTTPException(404, "GitHub webhooks are not configured")
        body = await request.body()
        expected = "sha256=" + _routes.hmac.new(secret.encode(), body, _routes.hashlib.sha256).hexdigest()
        if not _routes.hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise _routes.HTTPException(401, "invalid webhook signature")
        if request.headers.get("X-GitHub-Event") != "pull_request":
            return {"reconciled": 0}
        try:
            payload = _routes.json.loads(body)
            target = (str(payload["repository"]["full_name"]), int(payload["pull_request"]["number"]))
        except (ValueError, KeyError, TypeError):
            raise _routes.HTTPException(400, "not a pull request event") from None
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(_routes.merge_flow.reconcile, ctx, target)
        await run_in_threadpool(_routes.merge_flow.merge_ready, ctx)
        return {"reconciled": result["checked"]}

    review_queue.mount_human_reviews(app, deps.console_user, deps.optional_console_user)
    return SimpleNamespace(review_queue=review_queue, review_item_kind=review_item_kind,
                           current_review_item_kind=current_review_item_kind,
                           review_thread=review_thread, review_data=review_data)


def _review_metrics_csv(metrics: dict, project: str) -> _routes.PlainTextResponse:
    """Serialize the review metrics using the existing CSV column and row order."""
    output = _routes.io.StringIO(newline="")
    writer = _routes.csv.writer(output)
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
    return _routes.PlainTextResponse(
        output.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=human-review-metrics.csv"})


def install_review_export_routes(app, ctx, deps, review_data):
    @app.get("/v1/reviews/export.csv")
    def export_review_metrics(request: _routes.Request,
                              project: str = _routes.Query(default="")) -> _routes.PlainTextResponse:
        user = (deps.console_user(request, "view.human_review") if ctx.settings.console_login
                else deps.optional_console_user(request))
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            deps.audit("console", "access_denied", "", "", None, user.subject, False,
                       "not available for your role", {"path": request.url.path, "project": project,
                                                         "roles": list(user.effective_roles)})
            raise _routes.HTTPException(403, "not available for your role")
        metrics = review_data(project, scope)["metrics"]
        return _review_metrics_csv(metrics, project)

    return export_review_metrics


def install_review_approval_routes(app, ctx, deps, runtime):
    @app.post("/v1/reviews/{key}/approve")
    def approve_review(key: str, _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        ticket, run_id, fence, number = deps.reviewer_run(key)
        try:
            if not number:
                raise _routes.HTTPException(409, "ticket has no pull request to approve")
            run = deps.fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            ticket_repository_of(ctx).stamp_approved_at_if_empty((key,))
            result = deps.queued_merge(run, number, f"{key}: {ticket['summary']}",
                                       (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                # approved: the merge step of the flow takes over, so nobody has to press merge later
                note = (
                    f"Human review approval could not merge PR #{number} yet: {result.get('message', 'merge refused')}."
                    " The flow keeps the ticket as Ready to merge and merges it as soon as "
                    "the pull request can merge.")
                deps.reviewer_comment(key, run_id, fence, number, note)
                deps.audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                           result.get("message", "merge refused"), {"kind": "review_before_merge"})
                queued = ""
                try:
                    queued = deps.reviewer_transition(
                        key, run_id, fence, _routes.merge_flow.READY_TO_MERGE, "approved, merge pending")
                except Exception as error:  # noqa: BLE001 - Jira may lack the status; approval still queues it
                    runtime.log.info("%s stays in Human review until it merges: %s", key, error)
                deps.finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "queued": True, "status": queued or ticket["status"],
                        "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review approved in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            deps.reviewer_comment(key, run_id, fence, number, note)
            _routes.merge_flow.record_merge(
                ctx, key, _routes.merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow", settled=False)
            status = deps.reviewer_transition(key, run_id, fence, "Done", "human review approved")
            deps.record_reviewer_action(ticket, run_id, fence, "approve")
            deps.finish_reviewer_run(key, run_id, fence, "merged", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except _routes.HTTPException:
            deps.audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                       "review approval failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review approval failed for %s", key)
            deps.audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                       "review approval failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise _routes.HTTPException(502, "could not complete the review approval") from error

    @app.post("/v1/reviews/{key}/rework")
    def request_review_rework(key: str, body: _routes.ReviewerBody,
                              _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        comment = body.comment.strip()
        if not comment:
            raise _routes.HTTPException(400, "a rework comment is required")
        ticket, run_id, fence, number = deps.reviewer_run(key)
        try:
            note = f"Human review requested rework:\n\n{comment}"
            deps.reviewer_comment(key, run_id, fence, number, note)
            deps.reviewer_labels(key, run_id, fence, add=("rework",))
            status = deps.reviewer_transition(key, run_id, fence, "To Do", "human review requested rework")
            deps.record_reviewer_action(ticket, run_id, fence, "rework", comment)
            deps.finish_reviewer_run(key, run_id, fence, "rework", comment)
            return {"status": status}
        except _routes.HTTPException:
            deps.audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                       "review rework failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review rework failed for %s", key)
            deps.audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                       "review rework failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise _routes.HTTPException(502, "could not return the ticket for rework") from error

    return SimpleNamespace(approve_review=approve_review, request_review_rework=request_review_rework)


def install_review_rejection_and_question_routes(app, ctx, deps, runtime, review_item_kind, review_thread):
    @app.post("/v1/reviews/{key}/reject")
    def reject_review(key: str, body: _routes.ReviewerBody,
                      _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        reason = body.reason.strip()
        if not reason:
            raise _routes.HTTPException(400, "a rejection reason is required")
        ticket, run_id, fence, number = deps.reviewer_run(key)
        try:
            if not number:
                raise _routes.HTTPException(409, "ticket has no pull request to reject")
            note = f"Human review rejected this change.\n\nReason: {reason}"
            deps.reviewer_comment(key, run_id, fence, number, note)
            run = deps.fenced(run_id, fence, "github", "close", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            repository = deps.repository_for_run(run, project)
            gateway = runtime.github_gateway(ctx, repository)
            # A rejection is final: reconcile must not send the rejected ticket back because its PR is closed.
            ticket_repository_of(ctx).close_pull_request_on_ticket(((_routes.merge_flow.latest_pr(ctx.db, key) or {}).get('url', ''), f'rejected in human review: {reason}'[:500], key))
            closed = gateway.close_pr(repository.split("/", 1)[-1], number)
            _routes.publish_for(ctx, _routes.GitHubPrClosed(
                run_id=run_id, fence=fence, ticket=key, actor=run["account_id"], repo=repository, number=number))
            deps.reviewer_labels(key, run_id, fence, add=("rejected",), remove=("require-human",))
            status = deps.reviewer_transition(key, run_id, fence, "Done", "human review rejected")
            deps.record_reviewer_action(ticket, run_id, fence, "reject", reason)
            deps.finish_reviewer_run(key, run_id, fence, "rejected", reason)
            return {"status": status, "closed": bool(closed)}
        except _routes.HTTPException:
            deps.audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                       "review rejection failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review rejection failed for %s", key)
            deps.audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                       "review rejection failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise _routes.HTTPException(502, "could not reject the reviewed change") from error

    @app.post("/v1/reviews/{key}/ask-ai")
    def ask_review_ai(key: str, body: _routes.ReviewerBody,
                      _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        question = (body.question or body.comment).strip()
        if not question:
            raise _routes.HTTPException(400, "a question for the AI is required")
        ticket, run_id, fence, number = deps.reviewer_run(
            key, ("Human review", "Needs owner", "To Do", "In Progress", "In Verify"))
        try:
            note = f"Human review asked the AI:\n\n{question}"
            deps.reviewer_comment(key, run_id, fence, number, note)
            deps.reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = ticket["status"]
            if status != "Needs owner":
                status = deps.reviewer_transition(key, run_id, fence, "Needs owner", "reviewer asked the AI")
            _routes.escalations.raise_reviewer_question(ctx.db, key, ticket["project"], run_id, question)
            deps.record_reviewer_action(ticket, run_id, fence, "ask-ai", question)
            deps.finish_reviewer_run(key, run_id, fence, "question", question)
            return {"status": status, "thread": review_thread(key)}
        except _routes.HTTPException:
            deps.audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                       "AI question failed", {"kind": review_item_kind(ticket)})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review AI question failed for %s", key)
            deps.audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                       "AI question failed", {"kind": review_item_kind(ticket)})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise _routes.HTTPException(502, "could not send the question to the AI") from error

    return SimpleNamespace(reject_review=reject_review, ask_review_ai=ask_review_ai)


def install_review_answer_and_gate_routes(app, ctx, deps, runtime, review_item_kind):
    @app.post("/v1/reviews/{key}/answer")
    def answer_review_question(key: str, body: _routes.ReviewerBody,
                               _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        answer = (body.answer or body.comment).strip()
        if not answer:
            raise _routes.HTTPException(400, "an answer is required")
        ticket, run_id, fence, number = deps.reviewer_run(
            key, ("Needs owner",), allowed_kinds=("question_from_ai", "decision_needed"))
        try:
            deps.reviewer_comment(key, run_id, fence, number, f"Human answer to the AI:\n\n{answer}")
            deps.reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = deps.reviewer_transition(key, run_id, fence, "To Do", "human answered the AI question")
            deps.record_reviewer_action(ticket, run_id, fence, "answer", answer)
            deps.finish_reviewer_run(key, run_id, fence, "answered", answer)
            return {"status": status}
        except _routes.HTTPException:
            deps.audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                       "human answer failed", {"kind": review_item_kind(ticket)})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review answer failed for %s", key)
            deps.audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                       "human answer failed", {"kind": review_item_kind(ticket)})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise _routes.HTTPException(502, "could not record the answer") from error

    @app.post("/v1/reviews/{key}/skip-gate")
    def skip_review_gate(key: str, _reviewer: str = _routes.Depends(deps.reviewer_auth)) -> dict:
        ticket, run_id, fence, number = deps.reviewer_run(key)
        try:
            if "require-human" not in (ticket.get("labels") or []):
                raise _routes.HTTPException(409, "this ticket has no human review gate to skip")
            if not number:
                raise _routes.HTTPException(409, "ticket has no pull request to skip the gate for")
            run = deps.fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = deps.queued_merge(run, number, f"{key}: {ticket['summary']}",
                                       (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                note = f"Human review gate could not be skipped because PR #{number} did not merge: "
                note += result.get("message", "merge refused")
                deps.reviewer_comment(key, run_id, fence, number, note)
                deps.audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                           result.get("message", "merge refused"), {"kind": "review_before_merge"})
                deps.finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review gate skipped in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            deps.reviewer_comment(key, run_id, fence, number, note)
            deps.reviewer_labels(key, run_id, fence, remove=("require-human",))
            _routes.merge_flow.record_merge(
                ctx, key, _routes.merge_flow.latest_pr(ctx.db, key), result.get("sha", ""), "flow", settled=False)
            status = deps.reviewer_transition(key, run_id, fence, "Done", "human review gate skipped")
            deps.record_reviewer_action(ticket, run_id, fence, "skip-gate")
            deps.finish_reviewer_run(key, run_id, fence, "skipped_gate", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except _routes.HTTPException:
            deps.audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                       "review gate skip failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise
        except Exception as error:  # noqa: BLE001
            runtime.log.exception("human review gate skip failed for %s", key)
            deps.audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                       "review gate skip failed", {"kind": "review_before_merge"})
            deps.finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise _routes.HTTPException(502, "could not skip the human review gate") from error

    return SimpleNamespace(answer_review_question=answer_review_question, skip_review_gate=skip_review_gate)
