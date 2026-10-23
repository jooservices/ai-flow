"""Merge queue: ready pull requests of one repository merge one after another, each on top of the previous one.

Without a queue, two verified pull requests race: the second finds the base moved by the first and is sent back
(or fails the merge). A verify run that wants to merge calls `turn`; only the oldest waiting entry of the
repository has the turn. Everyone else is told its position and polls again. On its turn the pull request is
first brought up to date with the base (`advance`: GitHub merges the base in when that is clean), so it merges
"rebased on the previous one"; CI runs again on the new head before the merge goes through.

An entry stays `waiting` while its verify run is running; one whose run ended (died, lease lost) or that was silent
for STALE_AFTER_S is dropped so it cannot block the queue. Entries are kept as `merged` or
`left` history. docs/v4/merge-conflicts.md describes the whole flow.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("aiflow4.merge_queue")
STALE_AFTER_S = 1200  # a verify run waits for CI on the new head between two calls


def turn(db, repo: str, number: int, ticket_key: str, run_id: str = "") -> dict[str, Any]:
    """Join (or keep the place in) the queue of `repo`. Returns {"turn", "position", "ahead"}; position 1 = merge now."""
    db.run("UPDATE merge_queue q SET state='left' WHERE q.repo=%s AND q.state='waiting' AND q.number <> %s AND ("
           " q.touched_at < now() - %s * interval '1 second' OR (q.run_id <> '' AND NOT EXISTS ("
           " SELECT 1 FROM runs r WHERE r.id::text = q.run_id AND r.status = 'running')))",
           (repo, number, STALE_AFTER_S))
    db.run("INSERT INTO merge_queue (repo, number, ticket_key, run_id) VALUES (%s,%s,%s,%s)"
           " ON CONFLICT (repo, number) DO UPDATE SET touched_at=now(), run_id=EXCLUDED.run_id,"
           " ticket_key=EXCLUDED.ticket_key,"
           " state='waiting', enqueued_at=CASE WHEN merge_queue.state='waiting' THEN merge_queue.enqueued_at"
           " ELSE now() END", (repo, number, ticket_key, run_id))
    waiting = db.all("SELECT number, ticket_key FROM merge_queue WHERE repo=%s AND state='waiting'"
                     " ORDER BY enqueued_at, number", (repo,))
    numbers = [r["number"] for r in waiting]
    position = numbers.index(number) + 1
    return {"turn": position == 1, "position": position, "ahead": [r["ticket_key"] for r in waiting[:position - 1]]}


def finish(db, repo: str, number: int, merged: bool) -> None:
    db.run("UPDATE merge_queue SET state=%s, touched_at=now() WHERE repo=%s AND number=%s",
           ("merged" if merged else "left", repo, number))


def leave(db, repo: str, number: int) -> None:
    finish(db, repo, number, False)


def snapshot(db, repo: str = "") -> list[dict[str, Any]]:
    """The waiting entries in merge order (all repositories, or one)."""
    return db.all("SELECT repo, number, ticket_key, enqueued_at FROM merge_queue WHERE state='waiting'"
                  " AND (%s='' OR repo=%s) ORDER BY enqueued_at, number", (repo, repo))


def advance(github, repo_name: str, number: int) -> dict[str, Any]:
    """On the pull request's turn: bring its branch up to date with the base.

    Returns {"state": "current"} when nothing is behind, {"state": "updated", "head_sha"} after a clean update, or
    {"state": "conflict", "message"} when GitHub could not merge the base in (a real conflict)."""
    try:
        pr = github.pr(repo_name, number)
        base, head = (pr.get("base") or {}).get("ref"), (pr.get("head") or {}).get("ref")
        behind = int(github.compare_branches(repo_name, base, head).get("behind_by", 0) or 0) if base and head else 0
    except Exception as exc:  # noqa: BLE001 - without a comparison the merge itself reports a stale branch
        log.warning("merge queue: cannot compare PR #%s with its base: %s", number, exc)
        return {"state": "current", "head_sha": ""}
    if not behind:
        return {"state": "current", "head_sha": (pr.get("head") or {}).get("sha", "")}
    res = github.update_branch(repo_name, number)
    if not res.get("ok"):
        return {"state": "conflict", "message": res.get("message", "")}
    return {"state": "updated", "head_sha": github.pr(repo_name, number)["head"]["sha"]}


def merge_next(db, github, repo: str, number: int, ticket_key: str, run_id: str,
               merge: Callable[[], dict]) -> dict[str, Any]:
    """The whole queue step for one `/v1/github/merge` call. `merge()` performs the real merge."""
    repo_name = repo.split("/", 1)[-1]
    place = turn(db, repo, number, ticket_key, run_id)
    if not place["turn"]:
        return {"merged": False, "queued": True, "position": place["position"], "ahead": place["ahead"],
                "message": f"waiting in the merge queue behind {', '.join(place['ahead'])}"}
    step = advance(github, repo_name, number)
    if step["state"] == "conflict":
        leave(db, repo, number)
        return {"merged": False, "conflict": True, "message": step["message"] or "conflicts with the base"}
    if step["state"] == "updated":
        return {"merged": False, "updated": True, "head_sha": step["head_sha"], "mergeable_state": "unknown",
                "message": "branch brought up to date with the base; waiting for checks on the new head"}
    res = merge()
    if res.get("merged"):
        finish(db, repo, number, True)
    return res
