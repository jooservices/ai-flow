"""Merged means Done: the flow merges approved pull requests, and Jira follows GitHub whoever merges.

merge_ready  every 30 s: a ticket in `Ready to merge` (or approved in Human review and not merged yet) is merged by
             the flow as soon as its pull request can merge: the branch is updated when it is behind, the checks
             are awaited, the pull request is merged and the ticket moves to Done (cause `flow merge`). A branch
             that conflicts, or a failing check, sends the ticket to development with the reason as findings.
reconcile    every 60 s (and on every GitHub webhook when a secret is configured): every ticket with a pull request
             is compared with GitHub. Merged -> Done with the merge time and commit; closed without merge -> To Do
             with the reason; reopened -> In Verify (cause `reconcile`). Only a change of the pull request is acted
             on, so a ticket a person moved on purpose is not pushed back every minute.
disagreements  a fix that fails is retried every pass and listed on the Dashboard once the ticket and its pull
             request have disagreed for more than five minutes.

Every move records its cause in `ticket_moves`: `flow merge` (the merge step), `person` (a console review action or a
move made in Jira), `reconcile` (this loop) or `flow` (any other step of the flow). The control plane works under the
same ticket lease as a worker, so a merge never races with a run.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import J

log = logging.getLogger("aiflow4.merge_flow")

READY_TO_MERGE = "Ready to merge"
FLOW_MERGE, PERSON, RECONCILE, FLOW = "flow merge", "person", "reconcile", "flow"
CAUSES = (FLOW_MERGE, PERSON, RECONCILE, FLOW)
DISAGREEMENT_AFTER_S = 300
MERGE_EVERY_S = 30
RECONCILE_EVERY_S = 60  # a merge by anyone reaches Jira within one minute of the merge, plus the GitHub read
# a first look at a pull request leaves tickets alone that nobody was working on: history is not rewritten
_QUIET_WHEN_MERGED = {"To Do", "Backlog", "Intake"}
_BACK_WHEN_CLOSED = {"In Verify", "Human review", READY_TO_MERGE}
_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)")
MAX_PER_PASS = 200


def pr_ref(url: str) -> dict[str, Any] | None:
    m = _PR_URL.match(url or "")
    if not m:
        return None
    return {"url": url, "repository": f"{m.group(1)}/{m.group(2)}", "name": m.group(2), "number": int(m.group(3))}


def record_move(ctx, ticket: str, from_status: str, to_status: str, cause: str, detail: str = "") -> None:
    """Keep who or what moved a ticket, in the move table and on the ticket timeline."""
    ctx.db.run("INSERT INTO ticket_moves (ticket, from_status, to_status, cause, detail) VALUES (%s,%s,%s,%s,%s)",
               (ticket, from_status or "", to_status, cause, detail[:500]))
    ctx.events.emit("ticket.moved", ticket=ticket, outcome=to_status, reason=f"{cause}: {detail}"[:300],
                    cause=cause, from_status=from_status or "")


def moves(db, ticket: str, limit: int = 30) -> list[dict]:
    return db.all("SELECT ts, from_status, to_status, cause, detail FROM ticket_moves WHERE ticket=%s"
                  " ORDER BY id DESC LIMIT %s", (ticket, limit))


def facts(row: dict) -> dict[str, Any]:
    """The pull request link, merge commit and the time between approval and merge, as the ticket shows them."""
    approved, merged = row.get("approved_at"), row.get("merged_at")
    seconds = int((merged - approved).total_seconds()) if approved and merged and merged >= approved else None
    return {"pr_url": row.get("pr_url") or "", "pr_state": row.get("pr_state") or "",
            "approved_at": approved, "merged_at": merged, "merge_sha": row.get("merge_sha") or "",
            "merged_by": row.get("merged_by") or "", "approval_to_merge_s": seconds,
            "note": row.get("merge_note") or ""}


def latest_pr(db, key: str) -> dict[str, Any] | None:
    run = db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>'' ORDER BY started_at DESC LIMIT 1", (key,))
    return pr_ref(run["pr_url"]) if run else None


def _gateway(ctx, ref: dict[str, Any]):
    from . import api
    gateway = api.github_gateway(ctx, ref["repository"])
    if gateway is None:
        raise RuntimeError("GitHub is not connected")
    return gateway


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _comment(ctx, key: str, text: str) -> None:
    try:
        ctx.jira.comment(key, text)
    except Exception as e:  # noqa: BLE001 - the move itself is what counts
        log.warning("could not comment on %s: %s", key, e)


def _audit(ctx, target: str, action: str, key: str, accepted: bool, result: str, payload: dict | None = None) -> None:
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES (%s,%s,%s,'control:merge',%s,%s,%s)", (target, action, key, accepted, result[:500], J(payload or {})))


def _move(ctx, t: dict, target: str, cause: str, detail: str, *, done_at: datetime | None = None) -> str:
    """Move a ticket in Jira (when it is not there yet), in the mirror, and record the cause."""
    key = t["key"]
    live = ctx.jira.status(key)
    status = live if live == target else ctx.jira.transition_to(key, target)
    ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(%s, done_at, now())"
               " ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, done_at, key))
    _audit(ctx, "jira", "transition", key, True, status, {"to": target, "cause": cause, "detail": detail[:300]})
    record_move(ctx, key, t["status"], status, cause, detail)
    return status


def _note_run(ctx, key: str, project: str, outcome: str, summary: str, findings: str = "") -> None:
    """A finished control run: the next dev run receives its findings like those of a reviewer."""
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash,status,outcome,"
               "summary,details,ended_at) VALUES (%s,%s,%s,'qa','merge-flow','flow','control',0,'','finished',%s,%s,%s,now())",
               (str(uuid.uuid4()), key, project, outcome, summary[:2000], J({"findings": findings})))


def send_to_dev(ctx, t: dict, ref: dict[str, Any] | None, reason: str, cause: str = FLOW_MERGE) -> None:
    """The pull request cannot merge: back to development, with the reason on the ticket and the pull request."""
    key = t["key"]
    findings = f"- {reason}"
    try:
        ctx.jira.add_label(key, "rework")
        ctx.db.run("UPDATE tickets SET labels=CASE WHEN 'rework'=ANY(labels) THEN labels"
                   " ELSE array_append(labels,'rework') END WHERE key=%s", (key,))
    except Exception as e:  # noqa: BLE001
        log.warning("could not label %s: %s", key, e)
    _comment(ctx, key, "AI Flow · merge · back to development\nWhat: The pull request could not be merged.\n"
                       f"Why: {reason}\nNext: Back to To Do; the next dev run continues the branch with this reason.")
    if ref:
        try:
            _gateway(ctx, ref).comment(ref["name"], ref["number"], f"AI Flow merge: sent back to development.\n\n{reason}")
        except Exception as e:  # noqa: BLE001
            log.warning("could not comment on PR #%s: %s", ref["number"], e)
    _note_run(ctx, key, t["project"], "rework", f"merge blocked: {reason}", findings)
    _move(ctx, t, "To Do", cause, reason)
    ctx.db.run("UPDATE tickets SET approved_at=NULL, merge_note=%s WHERE key=%s", (reason[:500], key))


def _merge_facts(ctx, key: str, ref: dict[str, Any] | None, state: str, data: dict, merged_by: str = "") -> None:
    merged_at = _parse_time(data.get("merged_at")) or datetime.now(timezone.utc)
    sha = str(data.get("merge_commit_sha") or data.get("sha") or "")
    login = merged_by or str((data.get("merged_by") or {}).get("login") or "")
    ctx.db.run("UPDATE tickets SET pr_url=%s, pr_state=%s, merged_at=coalesce(merged_at,%s),"
               " merge_sha=CASE WHEN %s<>'' THEN %s ELSE merge_sha END,"
               " merged_by=CASE WHEN %s<>'' THEN %s ELSE merged_by END, merge_note='', pr_checked_at=now() WHERE key=%s",
               (ref["url"] if ref else "", state, merged_at, sha, sha, login, login, key))


def record_merge(ctx, key: str, ref: dict[str, Any] | None, sha: str, merged_by: str = "flow",
                 settled: bool = True) -> None:
    """The flow merged a pull request: keep the commit and the time (also the verify step's own merge).

    settled=False keeps the stored pull request state, so a ticket whose move to Done is still to come is not
    mistaken by reconcile for one that a person moved back on purpose."""
    ctx.db.run("UPDATE tickets SET pr_url=coalesce(nullif(%s,''),pr_url), pr_state=CASE WHEN %s THEN 'merged' ELSE pr_state END,"
               " merged_at=coalesce(merged_at,now()), merge_sha=%s, merged_by=%s, approved_at=coalesce(approved_at, now()),"
               " merge_note='', pr_checked_at=now() WHERE key=%s",
               (ref["url"] if ref else "", settled, sha, merged_by, key))
    ctx.db.run("DELETE FROM ticket_disagreements WHERE ticket=%s", (key,))


# --- the merge step ---------------------------------------------------------------------------------------------
def merge_ready(ctx) -> list[str]:
    """Merge every approved ticket whose pull request can merge; returns the keys merged in this pass."""
    if not ctx.jira:
        return []
    rows = ctx.db.all(
        "SELECT * FROM tickets WHERE status=%s OR (status='Human review' AND approved_at IS NOT NULL AND merged_at IS NULL)"
        " ORDER BY approved_at NULLS FIRST, key", (READY_TO_MERGE,))
    merged: list[str] = []
    for t in rows:
        try:
            if _merge_one(ctx, t):
                merged.append(t["key"])
        except Exception as e:  # noqa: BLE001 - one ticket must not stop the others
            log.warning("merge step for %s failed: %s", t["key"], e)
            ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s", (f"merge step failed: {e}"[:500], t["key"]))
    return merged


def _merge_one(ctx, t: dict) -> bool:
    key = t["key"]
    ref = latest_pr(ctx.db, key)
    if not ref:
        ctx.db.run("UPDATE tickets SET merge_note='waiting: the ticket has no pull request' WHERE key=%s", (key,))
        return False
    if not t["approved_at"]:
        ctx.db.run("UPDATE tickets SET approved_at=now() WHERE key=%s AND approved_at IS NULL", (key,))
    run_id = str(uuid.uuid4())
    claim = ctx.leases.claim(key, run_id, 120_000)
    if not claim.ok:
        return False  # a run owns the ticket now; the next pass looks again
    try:
        gateway = _gateway(ctx, ref)
        data = gateway.pr(ref["name"], ref["number"]) or {}
        if data.get("merged") or data.get("state") == "closed":
            return reconcile_one(ctx, t, ref, data)  # settled on GitHub already: follow it
        state = data.get("mergeable_state") or ""
        if state == "dirty":
            send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.")
            return False
        if state == "behind":
            res = gateway.update_branch(ref["name"], ref["number"])
            _audit(ctx, "github", "update-branch", key, bool(res.get("ok")), str(res.get("message", "")),
                   {"number": ref["number"]})
            if not res.get("ok") and "conflict" in str(res.get("message", "")).lower():
                send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.")
            else:
                ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s",
                           ("waiting: the branch was updated, the checks run again", key))
            return False
        head = (data.get("head") or {}).get("sha") or ""
        checks = gateway.checks(ref["name"], head) if head else []
        failed = [c for c in checks if c["state"] == "failure"]
        if failed:
            send_to_dev(ctx, t, ref, "Checks failed: " + ", ".join(f"{c['name']} ({c['url']})" for c in failed[:8]))
            return False
        if any(c["state"] == "pending" for c in checks):
            ctx.db.run("UPDATE tickets SET merge_note='waiting: the checks are still running' WHERE key=%s", (key,))
            return False
        from . import api
        project = ctx.repo().projects.get(t["project"])
        method = (getattr(project, "merge", None) or {}).get("method", "squash") if project else "squash"
        res = gateway.merge_pr(ref["name"], ref["number"], f"{key}: {t['summary']}", method)
        _audit(ctx, "github", "merge", key, bool(res.get("merged")), res.get("sha") or res.get("message", ""),
               {"number": ref["number"], "method": method, "repo": ref["repository"]})
        ctx.events.emit("github.merge", ticket=key, account="merge-flow", node="control",
                        outcome="merged" if res.get("merged") else "refused", reason=str(res.get("message", ""))[:200])
        if not res.get("merged"):
            if res.get("mergeable_state") == "dirty":
                send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.")
            else:
                ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s",
                           (f"waiting: GitHub refused the merge ({res.get('message', '')})"[:500], key))
            return False
        # the move first: a Jira failure leaves the ticket for reconcile, which still sees an unmerged-looking state
        _move(ctx, t, "Done", FLOW_MERGE, f"PR #{ref['number']} merged as {str(res.get('sha') or '')[:10]}")
        record_merge(ctx, key, ref, str(res.get("sha") or ""))
        row = ctx.db.one("SELECT * FROM tickets WHERE key=%s", (key,))
        seconds = facts(row)["approval_to_merge_s"]
        _comment(ctx, key, f"AI Flow · merge · merged\nWhat: PR #{ref['number']} merged as {str(res.get('sha') or '')[:10]}"
                           f" ({ref['url']}).\nWhy: The ticket was approved and the pull request could merge.\n"
                           f"Next: Ticket Done" + (f"; {seconds} s between approval and merge." if seconds is not None else "."))
        _note_run(ctx, key, t["project"], "merged", f"PR #{ref['number']} merged by the flow")
        return True
    finally:
        ctx.leases.release(key, run_id, claim.fence)


# --- the reconcile loop -----------------------------------------------------------------------------------------
def _github_state(data: dict) -> str:
    if data.get("merged"):
        return "merged"
    return "closed" if data.get("state") == "closed" else "open"


def reconcile_one(ctx, t: dict, ref: dict[str, Any], data: dict) -> bool:
    """Compare one ticket with its pull request. True when the ticket moved to Done."""
    key, status, prev = t["key"], t["status"], t["pr_state"] or ""
    state = _github_state(data)
    target, reason, cause_detail = "", "", ""
    if state == "merged":
        by = str((data.get("merged_by") or {}).get("login") or "")
        if status != "Done" and not (prev == "" and status in _QUIET_WHEN_MERGED) and prev != "merged":
            target, cause_detail = "Done", f"PR #{ref['number']} merged on GitHub" + (f" by {by}" if by else "")
    elif state == "closed":
        if prev != "closed" and (status in _BACK_WHEN_CLOSED or (status == "Done" and prev == "open")):
            target = "To Do"
            reason = f"PR #{ref['number']} was closed without being merged."
            cause_detail = reason
    elif prev == "closed" and status in ("To Do", "Done"):
        target, cause_detail = "In Verify", f"PR #{ref['number']} was reopened"
    if not target:
        _store_state(ctx, key, ref, state, data)
        ctx.db.run("DELETE FROM ticket_disagreements WHERE ticket=%s", (key,))
        return False
    try:
        # the move first, then the stored state: a failed move is tried again on the next pass
        if state == "merged":
            when = _parse_time(data.get("merged_at"))
            _move(ctx, t, "Done", RECONCILE, cause_detail, done_at=when)
            _merge_facts(ctx, key, ref, state, data)
            _comment(ctx, key, f"AI Flow · reconcile · Done\nWhat: PR #{ref['number']} ({ref['url']}) was merged on GitHub"
                               f"{' at ' + when.strftime('%Y-%m-%d %H:%M UTC') if when else ''}.\n"
                               "Why: Jira follows GitHub whoever merges.\nNext: Ticket Done.")
            _note_run(ctx, key, t["project"], "merged", cause_detail)
        elif state == "closed":
            _move(ctx, t, "To Do", RECONCILE, reason)
            _store_state(ctx, key, ref, state, data)
            _note_run(ctx, key, t["project"], "rework", "pull request closed without merge", f"- {reason}")
            ctx.db.run("UPDATE tickets SET approved_at=NULL, merge_note=%s WHERE key=%s", (reason, key))
            _comment(ctx, key, f"AI Flow · reconcile · back to To Do\nWhat: {reason}\nWhy: Jira follows GitHub; closed work is"
                               " not done.\nNext: The next dev run continues from the branch or opens a new pull request.")
        else:
            _move(ctx, t, "In Verify", RECONCILE, cause_detail)
            _store_state(ctx, key, ref, state, data)
            _comment(ctx, key, f"AI Flow · reconcile · back to In Verify\nWhat: {cause_detail}.\nWhy: Jira follows GitHub.\n"
                               "Next: A verifier reviews the pull request again.")
        ctx.db.run("DELETE FROM ticket_disagreements WHERE ticket=%s", (key,))
        return state == "merged"
    except Exception as e:  # noqa: BLE001 - retried on the next pass; the Dashboard lists it after five minutes
        log.warning("reconcile of %s failed: %s", key, e)
        _store_state(ctx, key, ref, prev, data, keep=True)
        ctx.db.run(
            "INSERT INTO ticket_disagreements (ticket, jira_status, github_state, expected, detail)"
            " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ticket) DO UPDATE SET jira_status=EXCLUDED.jira_status,"
            " github_state=EXCLUDED.github_state, expected=EXCLUDED.expected, detail=EXCLUDED.detail, updated_at=now()",
            (key, status, state, target, f"{cause_detail}; the fix failed: {e}"[:500]))
        return False


def _store_state(ctx, key: str, ref: dict[str, Any], state: str, data: dict, keep: bool = False) -> None:
    """Remember what GitHub says. A state is only stored once the ticket follows it (keep: leave it unchanged)."""
    if keep:
        ctx.db.run("UPDATE tickets SET pr_url=%s, pr_checked_at=now() WHERE key=%s", (ref["url"], key))
    elif state == "merged":
        _merge_facts(ctx, key, ref, state, data)
    else:
        ctx.db.run("UPDATE tickets SET pr_url=%s, pr_state=%s, pr_checked_at=now() WHERE key=%s", (ref["url"], state, key))


def reconcile(ctx, only: tuple[str, int] | None = None) -> dict[str, int]:
    """Compare every ticket that has a pull request with GitHub (only: one repository and pull request number)."""
    out = {"checked": 0, "moved": 0, "failed": 0}
    if not ctx.jira:
        return out
    rows = ctx.db.all(
        "SELECT t.*, r.pr_url AS latest_pr FROM tickets t JOIN LATERAL ("
        " SELECT pr_url FROM runs WHERE ticket_key=t.key AND pr_url<>'' ORDER BY started_at DESC LIMIT 1) r ON true"
        " WHERE t.type <> 'Epic' AND (t.pr_state <> 'merged' OR t.status <> 'Done' OR t.merged_at IS NULL)"
        " AND NOT (t.pr_state='closed' AND t.status='Done' AND t.pr_checked_at > now() - interval '10 minutes')"
        " ORDER BY t.pr_checked_at NULLS FIRST LIMIT %s", (MAX_PER_PASS,))
    for t in rows:
        ref = pr_ref(t["latest_pr"])
        if not ref or (only and (ref["repository"].lower(), ref["number"]) != (only[0].lower(), only[1])):
            continue
        if ref["url"] != t["pr_url"]:  # a new pull request for the ticket: its history starts again
            t["pr_state"] = ""
            ctx.db.run("UPDATE tickets SET pr_state='', pr_url=%s, merged_at=NULL, merge_sha='', merged_by='' WHERE key=%s",
                       (ref["url"], t["key"]))
        try:
            data = _gateway(ctx, ref).pr(ref["name"], ref["number"]) or {}
            before = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (t["key"],))["status"]
            out["checked"] += 1
            reconcile_one(ctx, t, ref, data)
            after = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (t["key"],))["status"]
            out["moved"] += int(before != after)
        except Exception as e:  # noqa: BLE001
            out["failed"] += 1
            log.warning("reconcile of %s could not read GitHub: %s", t["key"], e)
    return out


def disagreements(db, project_scope: list[str] | None = None) -> list[dict]:
    """Tickets whose Jira state and pull request have disagreed for more than five minutes."""
    rows = db.all(
        "SELECT d.ticket AS key, t.project, t.summary, d.jira_status, d.github_state, d.expected, d.detail, d.since,"
        " t.pr_url, extract(epoch FROM (now()-d.since))::int AS age_s FROM ticket_disagreements d"
        " JOIN tickets t ON t.key=d.ticket WHERE d.since < now() - make_interval(secs => %s) ORDER BY d.since",
        (DISAGREEMENT_AFTER_S,))
    return [r for r in rows if project_scope is None or r["project"] in project_scope]
