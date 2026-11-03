"""The steps of a stakeholder verdict: check the request, then record an acceptance or file UAT feedback."""
from typing import Any

from fastapi import HTTPException

from ...platform.events.wiring import publish_for
from ...stakeholder_screenshot import decode_screenshot
from .events import StakeholderAccepted, StakeholderBugFiled


def configured_project(ctx, key: str):
    project = ctx.repo().projects.get(key)
    if not project:
        raise HTTPException(404, f"unknown project {key}")
    return project


def recent_changes(ctx, project: str) -> list[dict[str, Any]]:
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


def check_verdict(ctx, key: str, body):
    """Validate a verdict request; returns (change, target ticket, sender, decoded screenshot)."""
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
    return change, target, sender, screenshot


def record_acceptance(ctx, log, key: str, body, target: str, sender: str, release_commit: str) -> dict:
    comment = (f"Comment nghiệm thu\nMôi trường: {body.environment}\n"
               f"Release hoặc commit: {release_commit}\nNgười gửi: {sender}")
    try:
        comment_id = ctx.jira.comment(target, comment)
    except Exception as exc:  # Jira adapters and HTTP clients can raise different transport exceptions.
        log.warning("Stakeholder acceptance comment failed for %s", target)
        raise HTTPException(502, "Jira could not record the acceptance") from exc
    publish_for(ctx, StakeholderAccepted(project=key, actor=sender, ticket=target, comment_id=comment_id,
                                         change=body.change_key, environment=body.environment))
    ctx.events.emit("stakeholder.acceptance", ticket=target, project=key, outcome="accepted",
                    account=sender, reason=body.environment)
    return {"ok": True, "verdict": body.verdict, "ticket": target, "comment_id": comment_id}


def file_uat_feedback(ctx, log, key: str, body, change: dict, sender: str, screenshot, page_label: str,
                      release_commit: str) -> dict:
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
    with ctx.db.transaction():
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (issue_key, key, ["uat-bug"], summary, description))
        publish_for(ctx, StakeholderBugFiled(project=key, actor=sender, ticket=issue_key, status=status,
                                             change=body.change_key, environment=body.environment,
                                             screenshot=bool(screenshot)))
    ctx.events.emit("stakeholder.feedback", ticket=issue_key, project=key, outcome="created",
                    account=sender, reason=body.environment, data={"related": body.change_key})
    return {"ok": True, "verdict": body.verdict, "key": issue_key, "status": status}
