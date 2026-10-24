"""Merged means Done: the flow merges approved pull requests, and Jira follows GitHub whoever merges.

merge_ready  every 30 s: a ticket in `Ready to merge` (or approved in Human review and not merged yet) is merged by
             the flow as soon as its pull request can merge: the branch is updated when it is behind, the checks
             are awaited, the pull request is merged and the ticket moves to Done (cause `flow merge`). A branch
             that conflicts, or a failing check, sends the ticket to development with the reason as findings.
reconcile    a pass every 20 s in its own thread (and on every GitHub webhook when a secret is configured): every ticket
             with a pull request is compared with GitHub. Merged -> Done with the merge time and commit, whatever
             state Jira is in; closed without merge -> To Do with the reason; reopened -> In Verify (cause
             `reconcile`). A pass stops starting work after 20 s and takes the least recently checked pull
             requests first, so a backlog is rotated and every pull request is looked at within 40 s (the bound
             is 60 s, RECONCILE_BOUND_S). Only a change of a pull request closed or reopened is acted on, so a
             ticket a person moved on purpose is not pushed back every minute.
disagreements  a fix that fails is retried every pass and listed on the Dashboard once the ticket and its pull
             request have disagreed for more than five minutes. The record is cleared only when the ticket is at
             the state GitHub implies.

Every move records its cause in `ticket_moves`: `flow merge` (the merge step), `person` (a console review action or a
move made in Jira), `reconcile` (this loop) or `flow` (any other step of the flow, the watchdog's requeues included).
The control plane writes Jira under the same ticket lease as a worker (claimed, and renewed before every outside
call), so a merge or a reconcile never races with a run that still holds a valid fence: when a run owns the ticket the
write waits for the next pass.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from .db import J

log = logging.getLogger("aiflow4.merge_flow")

READY_TO_MERGE = "Ready to merge"
FLOW_MERGE, PERSON, RECONCILE, FLOW = "flow merge", "person", "reconcile", "flow"
CAUSES = (FLOW_MERGE, PERSON, RECONCILE, FLOW)
DISAGREEMENT_AFTER_S = 300
MERGE_EVERY_S = 30
RECONCILE_EVERY_S = 20    # start of a pass to the start of the next
RECONCILE_BUDGET_S = 20   # a pass starts no new pull request after this; the rest go first in the next pass
RECONCILE_BOUND_S = 60    # a merge by anyone reaches Jira within one minute: every + budget + the slowest call < bound
RECONCILE_WORKERS = 4
LEASE_TTL_MS = 120_000
_BACK_WHEN_CLOSED = {"In Verify", "Human review", READY_TO_MERGE}
_PR_URL = re.compile(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)")
MAX_PER_PASS = 500


class LeaseLost(RuntimeError):
    """The ticket lease expired or was taken while the control plane was writing: stop, the next pass starts again."""


class Hold:
    """The ticket lease held by the control plane for one write sequence, renewed before every outside call."""

    def __init__(self, ctx, key: str) -> None:
        self.ctx, self.key, self.run_id = ctx, key, str(uuid.uuid4())
        claim = ctx.leases.claim(key, self.run_id, LEASE_TTL_MS)
        self.ok, self.fence = claim.ok, claim.fence

    def keep(self) -> None:
        if not self.ctx.leases.renew(self.key, self.run_id, self.fence, LEASE_TTL_MS):
            raise LeaseLost(f"the lease of {self.key} was lost")

    def release(self) -> None:
        if self.ok:
            self.ctx.leases.release(self.key, self.run_id, self.fence)

    def __enter__(self) -> "Hold":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _keep(hold: Hold | None) -> None:
    if hold:
        hold.keep()


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


def _move(ctx, t: dict, target: str, cause: str, detail: str, *, done_at: datetime | None = None,
          hold: Hold | None = None) -> str:
    """Move a ticket in Jira (when it is not there yet), in the mirror, and record the cause.

    Raises when Jira does not end at the target, so nobody treats the ticket as moved."""
    key = t["key"]
    _keep(hold)
    live = ctx.jira.status(key)
    status = live if live == target else ctx.jira.transition_to(key, target)
    if status != target:
        raise RuntimeError(f"Jira is at {status}, not {target}")
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


def send_to_dev(ctx, t: dict, ref: dict[str, Any] | None, reason: str, cause: str = FLOW_MERGE,
                hold: Hold | None = None) -> None:
    """The pull request cannot merge: back to development, with the reason on the ticket and the pull request."""
    key = t["key"]
    findings = f"- {reason}"
    _keep(hold)
    try:
        ctx.jira.add_label(key, "rework")
        ctx.db.run("UPDATE tickets SET labels=CASE WHEN 'rework'=ANY(labels) THEN labels"
                   " ELSE array_append(labels,'rework') END WHERE key=%s", (key,))
    except Exception as e:  # noqa: BLE001
        log.warning("could not label %s: %s", key, e)
    _comment(ctx, key, "AI Flow · merge · back to development\nWhat: The pull request could not be merged.\n"
                       f"Why: {reason}\nNext: Back to To Do; the next dev run continues the branch with this reason.")
    if ref:
        _keep(hold)
        try:
            _gateway(ctx, ref).comment(ref["name"], ref["number"], f"AI Flow merge: sent back to development.\n\n{reason}")
        except Exception as e:  # noqa: BLE001
            log.warning("could not comment on PR #%s: %s", ref["number"], e)
    _note_run(ctx, key, t["project"], "rework", f"merge blocked: {reason}", findings)
    _move(ctx, t, "To Do", cause, reason, hold=hold)
    ctx.db.run("UPDATE tickets SET approved_at=NULL, merge_note=%s WHERE key=%s", (reason[:500], key))


def _merge_facts(ctx, key: str, ref: dict[str, Any] | None, state: str, data: dict, merged_by: str = "") -> None:
    # GitHub's own merge time wins over one stored earlier; without it the stored time stays
    github_time = _parse_time(data.get("merged_at"))
    sha = str(data.get("merge_commit_sha") or data.get("sha") or "")
    login = merged_by or str((data.get("merged_by") or {}).get("login") or "")
    ctx.db.run("UPDATE tickets SET pr_url=%s, pr_state=%s, merged_at=coalesce(%s, merged_at, now()),"
               " merge_sha=CASE WHEN %s<>'' THEN %s ELSE merge_sha END,"
               " merged_by=CASE WHEN %s<>'' THEN %s ELSE merged_by END, merge_note='', pr_checked_at=now() WHERE key=%s",
               (ref["url"] if ref else "", state, github_time, sha, sha, login, login, key))


def record_merge(ctx, key: str, ref: dict[str, Any] | None, sha: str, merged_by: str = "flow",
                 settled: bool = True, merged_at: datetime | None = None) -> None:
    """The flow merged a pull request: keep the commit and the time (also the verify step's own merge).

    merged_at is the time of the merge itself (GitHub's, or the clock right after the merge call), never the time of
    the Jira move that follows. settled=False keeps the stored pull request state until the move to Done is made."""
    ctx.db.run("UPDATE tickets SET pr_url=coalesce(nullif(%s,''),pr_url), pr_state=CASE WHEN %s THEN 'merged' ELSE pr_state END,"
               " merged_at=coalesce(%s, merged_at, now()), merge_sha=%s, merged_by=%s,"
               " approved_at=coalesce(approved_at, now()), merge_note='', pr_checked_at=now() WHERE key=%s",
               (ref["url"] if ref else "", settled, merged_at, sha, merged_by, key))
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
    with Hold(ctx, key) as hold:
        if not hold.ok:
            return False  # a run owns the ticket now; the next pass looks again
        return _merge_held(ctx, t, ref, hold)


def _merge_held(ctx, t: dict, ref: dict[str, Any], hold: Hold) -> bool:
    key = t["key"]
    gateway = _gateway(ctx, ref)
    hold.keep()
    data = gateway.pr(ref["name"], ref["number"]) or {}
    if data.get("merged") or data.get("state") == "closed":
        return reconcile_one(ctx, t, ref, data, hold=hold)  # settled on GitHub already: follow it
    state = data.get("mergeable_state") or ""
    if state == "dirty":
        send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.", hold=hold)
        return False
    if state == "behind":
        hold.keep()
        res = gateway.update_branch(ref["name"], ref["number"])
        _audit(ctx, "github", "update-branch", key, bool(res.get("ok")), str(res.get("message", "")),
               {"number": ref["number"]})
        if not res.get("ok") and "conflict" in str(res.get("message", "")).lower():
            send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.", hold=hold)
        else:
            ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s",
                       ("waiting: the branch was updated, the checks run again", key))
        return False
    head = (data.get("head") or {}).get("sha") or ""
    hold.keep()
    checks = gateway.checks(ref["name"], head) if head else []
    failed = [c for c in checks if c["state"] == "failure"]
    if failed:
        send_to_dev(ctx, t, ref, "Checks failed: " + ", ".join(f"{c['name']} ({c['url']})" for c in failed[:8]), hold=hold)
        return False
    if any(c["state"] == "pending" for c in checks):
        ctx.db.run("UPDATE tickets SET merge_note='waiting: the checks are still running' WHERE key=%s", (key,))
        return False
    project = ctx.repo().projects.get(t["project"])
    method = (getattr(project, "merge", None) or {}).get("method", "squash") if project else "squash"
    hold.keep()
    # pinned to the commit whose checks were read: a branch that moved on is refused by GitHub and checked again
    res = gateway.merge_pr(ref["name"], ref["number"], f"{key}: {t['summary']}", method, sha=head)
    merge_clock = datetime.now(timezone.utc)
    _audit(ctx, "github", "merge", key, bool(res.get("merged")), res.get("sha") or res.get("message", ""),
           {"number": ref["number"], "method": method, "repo": ref["repository"], "pinned": head})
    ctx.events.emit("github.merge", ticket=key, account="merge-flow", node="control",
                    outcome="merged" if res.get("merged") else "refused", reason=str(res.get("message", ""))[:200])
    if not res.get("merged"):
        if res.get("mergeable_state") == "dirty":
            send_to_dev(ctx, t, ref, "The branch conflicts with the base branch.", hold=hold)
        elif str(res.get("message", "")).startswith("409"):
            ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s",
                       ("waiting: the branch changed after its checks were read; it is checked again", key))
        else:
            ctx.db.run("UPDATE tickets SET merge_note=%s WHERE key=%s",
                       (f"waiting: GitHub refused the merge ({res.get('message', '')})"[:500], key))
        return False
    # the merge time is the merge itself, kept before the Jira move: GitHub's own time when it can be read,
    # else the clock right after the merge call
    merged_at = merge_clock
    try:
        hold.keep()
        merged_at = _parse_time((gateway.pr(ref["name"], ref["number"]) or {}).get("merged_at")) or merge_clock
    except LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the merge time of PR #%s: %s", ref["number"], e)
    sha = str(res.get("sha") or "")
    record_merge(ctx, key, ref, sha, settled=False, merged_at=merged_at)
    _move(ctx, t, "Done", FLOW_MERGE, f"PR #{ref['number']} merged as {sha[:10]}", done_at=merged_at, hold=hold)
    ctx.db.run("UPDATE tickets SET pr_state='merged' WHERE key=%s", (key,))
    row = ctx.db.one("SELECT * FROM tickets WHERE key=%s", (key,))
    seconds = facts(row)["approval_to_merge_s"]
    hold.keep()
    _comment(ctx, key, f"AI Flow · merge · merged\nWhat: PR #{ref['number']} merged as {sha[:10]}"
                       f" ({ref['url']}).\nWhy: The ticket was approved and the pull request could merge.\n"
                       f"Next: Ticket Done" + (f"; {seconds} s between approval and merge." if seconds is not None else "."))
    _note_run(ctx, key, t["project"], "merged", f"PR #{ref['number']} merged by the flow")
    return True


# --- the reconcile loop -----------------------------------------------------------------------------------------
def _github_state(data: dict) -> str:
    if data.get("merged"):
        return "merged"
    return "closed" if data.get("state") == "closed" else "open"


def _disagree(ctx, key: str, status: str, state: str, target: str, detail: str) -> None:
    ctx.db.run(
        "INSERT INTO ticket_disagreements (ticket, jira_status, github_state, expected, detail)"
        " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ticket) DO UPDATE SET jira_status=EXCLUDED.jira_status,"
        " github_state=EXCLUDED.github_state, expected=EXCLUDED.expected, detail=EXCLUDED.detail, updated_at=now()",
        (key, status, state, target, detail[:500]))


def reconcile_one(ctx, t: dict, ref: dict[str, Any], data: dict, hold: Hold | None = None) -> bool:
    """Compare one ticket with its pull request. True when the ticket moved to Done.

    A merged pull request puts the ticket in Done from whatever state Jira is in. The Jira write is made under the
    ticket lease (`hold`, or claimed here): when a run owns the ticket, nothing is written and the next pass tries again."""
    key, status, prev = t["key"], t["status"], t["pr_state"] or ""
    state = _github_state(data)
    target, reason, cause_detail = "", "", ""
    if state == "merged":
        by = str((data.get("merged_by") or {}).get("login") or "")
        if status != "Done":
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
        ctx.db.run("DELETE FROM ticket_disagreements WHERE ticket=%s", (key,))  # the ticket is where GitHub says
        return False
    own = hold is None
    hold = hold or Hold(ctx, key)
    try:
        if not hold.ok:
            _store_state(ctx, key, ref, prev, data, keep=True)
            _disagree(ctx, key, status, state, target, f"{cause_detail}; a run holds the ticket, tried again next pass")
            return False
        return _reconcile_move(ctx, t, ref, data, state, target, reason, cause_detail, hold)
    except Exception as e:  # noqa: BLE001 - retried on the next pass; the Dashboard lists it after five minutes
        log.warning("reconcile of %s failed: %s", key, e)
        _store_state(ctx, key, ref, prev, data, keep=True)
        _disagree(ctx, key, status, state, target, f"{cause_detail}; the fix failed: {e}")
        return False
    finally:
        if own:
            hold.release()


def _reconcile_move(ctx, t: dict, ref: dict[str, Any], data: dict, state: str, target: str, reason: str,
                    cause_detail: str, hold: Hold) -> bool:
    key = t["key"]
    # the move first, then the stored state: a failed move is tried again on the next pass
    if state == "merged":
        when = _parse_time(data.get("merged_at"))
        _move(ctx, t, "Done", RECONCILE, cause_detail, done_at=when, hold=hold)
        _merge_facts(ctx, key, ref, state, data)
        hold.keep()
        _comment(ctx, key, f"AI Flow · reconcile · Done\nWhat: PR #{ref['number']} ({ref['url']}) was merged on GitHub"
                           f"{' at ' + when.strftime('%Y-%m-%d %H:%M UTC') if when else ''}.\n"
                           "Why: Jira follows GitHub whoever merges.\nNext: Ticket Done.")
        _note_run(ctx, key, t["project"], "merged", cause_detail)
    elif state == "closed":
        _move(ctx, t, "To Do", RECONCILE, reason, hold=hold)
        _store_state(ctx, key, ref, state, data)
        _note_run(ctx, key, t["project"], "rework", "pull request closed without merge", f"- {reason}")
        ctx.db.run("UPDATE tickets SET approved_at=NULL, merge_note=%s WHERE key=%s", (reason, key))
        hold.keep()
        _comment(ctx, key, f"AI Flow · reconcile · back to To Do\nWhat: {reason}\nWhy: Jira follows GitHub; closed work is"
                           " not done.\nNext: The next dev run continues from the branch or opens a new pull request.")
    else:
        _move(ctx, t, "In Verify", RECONCILE, cause_detail, hold=hold)
        _store_state(ctx, key, ref, state, data)
        hold.keep()
        _comment(ctx, key, f"AI Flow · reconcile · back to In Verify\nWhat: {cause_detail}.\nWhy: Jira follows GitHub.\n"
                           "Next: A verifier reviews the pull request again.")
    ctx.db.run("DELETE FROM ticket_disagreements WHERE ticket=%s", (key,))  # only after Jira reached the target
    return state == "merged"


def _store_state(ctx, key: str, ref: dict[str, Any], state: str, data: dict, keep: bool = False) -> None:
    """Remember what GitHub says. A state is only stored once the ticket follows it (keep: leave it unchanged)."""
    if keep:
        ctx.db.run("UPDATE tickets SET pr_url=%s, pr_checked_at=now() WHERE key=%s", (ref["url"], key))
    elif state == "merged":
        _merge_facts(ctx, key, ref, state, data)
    else:
        ctx.db.run("UPDATE tickets SET pr_url=%s, pr_state=%s, pr_checked_at=now() WHERE key=%s", (ref["url"], state, key))


def _reconcile_ticket(ctx, t: dict, ref: dict[str, Any]) -> tuple[int, int, int]:
    """One pull request of a pass: (checked, moved, failed)."""
    if ref["url"] != t["pr_url"]:  # a new pull request for the ticket: its history starts again
        t["pr_state"] = ""
        ctx.db.run("UPDATE tickets SET pr_state='', pr_url=%s, merged_at=NULL, merge_sha='', merged_by='' WHERE key=%s",
                   (ref["url"], t["key"]))
    try:
        data = _gateway(ctx, ref).pr(ref["name"], ref["number"]) or {}
        before = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (t["key"],))["status"]
        reconcile_one(ctx, t, ref, data)
        after = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (t["key"],))["status"]
        return 1, int(before != after), 0
    except Exception as e:  # noqa: BLE001
        log.warning("reconcile of %s could not read GitHub: %s", t["key"], e)
        # looked at, so a pull request GitHub cannot answer for does not keep the front of the queue
        ctx.db.run("UPDATE tickets SET pr_checked_at=now() WHERE key=%s", (t["key"],))
        return 0, 0, 1


def reconcile(ctx, only: tuple[str, int] | None = None, *, budget_s: float = RECONCILE_BUDGET_S,
              workers: int = RECONCILE_WORKERS, clock: Callable[[], float] = time.monotonic) -> dict[str, int]:
    """Compare every ticket that has a pull request with GitHub (only: one repository and pull request number).

    The pull requests checked longest ago go first and several are read at once. After `budget_s` no new pull
    request is started (`skipped` counts them); they head the next pass, so a backlog never starves one ticket and
    a merge is followed within RECONCILE_EVERY_S + RECONCILE_BUDGET_S, well inside RECONCILE_BOUND_S."""
    out = {"checked": 0, "moved": 0, "failed": 0, "skipped": 0}
    if not ctx.jira:
        return out
    deadline = clock() + budget_s
    rows = ctx.db.all(
        "SELECT t.*, r.pr_url AS latest_pr FROM tickets t JOIN LATERAL ("
        " SELECT pr_url FROM runs WHERE ticket_key=t.key AND pr_url<>'' ORDER BY started_at DESC LIMIT 1) r ON true"
        " WHERE t.type <> 'Epic' AND (t.pr_state <> 'merged' OR t.status <> 'Done' OR t.merged_at IS NULL)"
        " AND NOT (t.pr_state='closed' AND t.status='Done' AND t.pr_checked_at > now() - interval '10 minutes')"
        " ORDER BY t.pr_checked_at NULLS FIRST, t.key LIMIT %s", (MAX_PER_PASS,))
    todo = []
    for t in rows:
        ref = pr_ref(t["latest_pr"])
        if ref and not (only and (ref["repository"].lower(), ref["number"]) != (only[0].lower(), only[1])):
            todo.append((t, ref))

    def one(item: tuple[dict, dict]) -> tuple[int, int, int]:
        if clock() >= deadline:
            return -1, 0, 0
        return _reconcile_ticket(ctx, *item)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for checked, moved, failed in pool.map(one, todo):
            if checked < 0:
                out["skipped"] += 1
            else:
                out["checked"] += checked
                out["moved"] += moved
                out["failed"] += failed
    return out


def disagreements(db, project_scope: list[str] | None = None) -> list[dict]:
    """Tickets whose Jira state and pull request have disagreed for more than five minutes."""
    rows = db.all(
        "SELECT d.ticket AS key, t.project, t.summary, d.jira_status, d.github_state, d.expected, d.detail, d.since,"
        " t.pr_url, extract(epoch FROM (now()-d.since))::int AS age_s FROM ticket_disagreements d"
        " JOIN tickets t ON t.key=d.ticket WHERE d.since < now() - make_interval(secs => %s) ORDER BY d.since",
        (DISAGREEMENT_AFTER_S,))
    return [r for r in rows if project_scope is None or r["project"] in project_scope]
