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
from contextlib import contextmanager
from typing import Any, Callable, Iterator

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


def advance(github, repo_name: str, number: int, before_call: Callable[[], None] | None = None) -> dict[str, Any]:
    """On the pull request's turn: bring its branch up to date with the base.

    Returns {"state": "current"} when nothing is behind, {"state": "updated", "head_sha"} after a clean update, or
    {"state": "conflict", "message"} when GitHub reports a merge conflict, or {"state": "error", "message"} for any
    other failure (permission, service): that is not a conflict. `before_call` lets the control merge flow renew its
    ticket lease before each GitHub request."""
    if before_call:
        before_call()
    try:
        pr = github.pr(repo_name, number)
    except Exception as exc:  # noqa: BLE001 - without a comparison the merge itself reports a stale branch
        log.warning("merge queue: cannot compare PR #%s with its base: %s", number, exc)
        return {"state": "current", "head_sha": ""}
    base, head = (pr.get("base") or {}).get("ref"), (pr.get("head") or {}).get("ref")
    behind = 0
    if base and head:
        if before_call:
            before_call()
        try:
            behind = int(github.compare_branches(repo_name, base, head).get("behind_by", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - the merge itself reports a stale branch
            log.warning("merge queue: cannot compare PR #%s with its base: %s", number, exc)
            return {"state": "current", "head_sha": (pr.get("head") or {}).get("sha", "")}
    # GitHub may already report the PR as behind before compare metadata is populated.
    if pr.get("mergeable_state") == "behind":
        behind = max(behind, 1)
    if not behind:
        return {"state": "current", "head_sha": (pr.get("head") or {}).get("sha", "")}
    if before_call:
        before_call()
    res = github.update_branch(repo_name, number)
    if res.get("ok"):
        if before_call:
            before_call()
        return {"state": "updated", "head_sha": github.pr(repo_name, number)["head"]["sha"]}
    if res.get("conflict"):  # only a real merge conflict goes back to a developer AI
        return {"state": "conflict", "message": res.get("message", "")}
    return {"state": "error", "message": res.get("message", "") or "the branch update failed"}


@contextmanager
def repo_lock(db, repo: str) -> Iterator[bool]:
    """Hold the merge lock of one repository from the queue turn through the merge (False: another call holds it).

    A session advisory lock on its own connection: it is released here, and by PostgreSQL when the control process
    dies, so a crashed merge never blocks the repository."""
    key = f"aiflow4-merge-queue:{repo}"
    with db.pool.connection() as conn:
        got = bool(conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (key,)).fetchone()["got"])
        try:
            yield got
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


def _waiting(place: dict[str, Any], message: str = "") -> dict[str, Any]:
    ahead = ", ".join(place["ahead"])
    return {"merged": False, "queued": True, "position": place["position"], "ahead": place["ahead"],
            "message": message or f"waiting in the merge queue behind {ahead}"}


def merge_next(db, github, repo: str, number: int, ticket_key: str, run_id: str,
               merge: Callable[[], dict], before_call: Callable[[], None] | None = None) -> dict[str, Any]:
    """The whole queue step for one `/v1/github/merge` call. `merge()` performs the real merge.

    The turn, the branch update and the merge run under the repository lock, so two concurrent calls can never
    both merge: one that finds the lock taken is told to wait and asks again."""
    repo_name = repo.split("/", 1)[-1]
    place = turn(db, repo, number, ticket_key, run_id)
    if not place["turn"]:
        return _waiting(place)
    with repo_lock(db, repo) as locked:
        if not locked:
            return _waiting(place, "another merge of this repository is in progress; waiting for it")
        place = turn(db, repo, number, ticket_key, run_id)  # the queue may have changed while the lock was taken
        if not place["turn"]:
            return _waiting(place)
        step = advance(github, repo_name, number, before_call)
        if step["state"] == "conflict":
            leave(db, repo, number)
            return {"merged": False, "conflict": True, "message": step["message"] or "conflicts with the base"}
        if step["state"] == "error":
            # permission or service failure: not a conflict, so the work is not sent back to a developer AI
            leave(db, repo, number)
            return {"merged": False, "update_failed": True,
                    "message": "the branch could not be brought up to date: " + step["message"]}
        if step["state"] == "updated":
            return {"merged": False, "updated": True, "head_sha": step["head_sha"], "mergeable_state": "unknown",
                    "message": "branch brought up to date with the base; waiting for checks on the new head"}
        if before_call:
            before_call()
        res = merge()
        if res.get("merged"):
            finish(db, repo, number, True)
        return res
