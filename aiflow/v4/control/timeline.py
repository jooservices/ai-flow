"""Ticket timeline: where a ticket's time went, computed from the registry's events and runs (not Jira worklogs).

Segments (ordered by start, never overlapping):
  queued   the ticket waits for an AI: To Do with aiflow-ready, or In Verify, while no run works on it
  dev      one dev run (claim to result)
  rework   one dev run after a verify sent the ticket back; `round` counts the rework rounds
  verify   one verify run
  ai       a run of another step (intake, master, qa)
  waiting  the ticket waits on a person: Needs owner or Human review
Status changes come from `jira.transition` events (moves made through the control API or watchdog) and `ticket.status`
events (moves and label changes made by people, seen by jira_sync, at most one sync late). Completed tickets use Jira's
stored resolution time when available, so a delayed sync does not extend lead time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

READY = "aiflow-ready"
WAITING = {"Needs owner", "Human review"}
STATUS_EVENTS = ("ticket.status", "jira.transition")


def _secs(start: datetime, end: datetime) -> int:
    return max(0, int(round((end - start).total_seconds())))


def _status_changes(db, key: str, ready_now: bool) -> list[dict]:
    """[{ts, status, ready}] in order; `ready` carries forward when an event does not say (jira.transition)."""
    rows = db.all("SELECT ts, type, outcome, data FROM events WHERE ticket=%s AND type = ANY(%s) AND outcome <> ''"
                  " ORDER BY ts, id", (key, list(STATUS_EVENTS)))
    known = [r["data"].get("ready") for r in rows if r["type"] == "ticket.status" and "ready" in (r["data"] or {})]
    ready = bool(known[0]) if known else ready_now
    out = []
    for r in rows:
        if r["type"] == "ticket.status" and "ready" in (r["data"] or {}):
            ready = bool(r["data"]["ready"])
        out.append({"ts": r["ts"], "status": r["outcome"], "ready": ready})
    return out


def _status_kind(status: str, ready: bool) -> str | None:
    if status in WAITING:
        return "waiting"
    if (status == "To Do" and ready) or status == "In Verify":
        return "queued"
    return None


def _minus(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """The parts of [start, end) not covered by the (sorted) busy intervals."""
    out, cur = [], start
    for b0, b1 in busy:
        if b1 <= cur or b0 >= end:
            continue
        if b0 > cur:
            out.append((cur, b0))
        cur = max(cur, b1)
    if cur < end:
        out.append((cur, end))
    return out


def _run_segments(runs: list[dict], now: datetime) -> list[dict]:
    segs, rounds = [], 0
    for r in runs:
        end = r["ended_at"] or (now if r["status"] == "running" else r["heartbeat_at"])
        kind = {"dev": "rework" if rounds else "dev", "verify": "verify"}.get(r["step"], "ai")
        outcome = r["outcome"] or r["status"]
        seg = {"kind": kind, "start": r["started_at"], "end": max(end, r["started_at"]), "step": r["step"],
               "run_id": str(r["id"]), "account": r["account_id"], "outcome": outcome}
        if kind == "rework":
            seg["round"] = rounds
        segs.append(seg)
        if r["step"] == "verify" and r["outcome"] == "rework":
            rounds += 1
    return segs


def timeline(db, key: str, now: datetime | None = None) -> dict[str, Any] | None:
    t = db.one("SELECT key, status, labels, done_at FROM tickets WHERE key=%s", (key,))
    if not t:
        return None
    now = now or datetime.now(timezone.utc)
    runs = db.all("SELECT id, step, account_id, status, outcome, started_at, ended_at, heartbeat_at FROM runs"
                  " WHERE ticket_key=%s ORDER BY started_at", (key,))
    changes = _status_changes(db, key, READY in (t["labels"] or []))
    run_segs = _run_segments(runs, now)

    done_at = t["done_at"] if t["status"] == "Done" else None
    if t["status"] == "Done" and done_at is None:
        done = [c["ts"] for c in changes if c["status"] == "Done"]
        done_at = done[-1] if done else max([s["end"] for s in run_segs] + [c["ts"] for c in changes], default=None)
    end = done_at or now

    busy = sorted((s["start"], s["end"]) for s in run_segs)
    segs = list(run_segs)
    for i, c in enumerate(changes):
        kind = _status_kind(c["status"], c["ready"])
        c_end = min(changes[i + 1]["ts"] if i + 1 < len(changes) else end, end)
        if not kind or c["ts"] >= c_end:
            continue
        for s0, s1 in _minus(c["ts"], c_end, busy):
            segs.append({"kind": kind, "start": s0, "end": s1, "status": c["status"]})
    segs.sort(key=lambda s: (s["start"], s["end"]))

    ready_at = [c["ts"] for c in changes if _status_kind(c["status"], c["ready"]) == "queued" and c["status"] == "To Do"]
    started = ready_at[0] if ready_at else min([s["start"] for s in segs] + [c["ts"] for c in changes], default=None)

    by_kind: dict[str, int] = {}
    for s in segs:
        s["seconds"] = _secs(s["start"], s["end"])
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + s["seconds"]
    by_outcome: dict[str, int] = {}
    for s in run_segs:
        by_outcome[s["outcome"]] = by_outcome.get(s["outcome"], 0) + 1
    totals = {
        "lead_time_s": _secs(started, end) if started else 0,
        "ai_s": sum(by_kind.get(k, 0) for k in ("dev", "rework", "verify", "ai")),
        "queue_s": by_kind.get("queued", 0), "waiting_s": by_kind.get("waiting", 0),
        "rework_rounds": sum(1 for r in runs if r["step"] == "verify" and r["outcome"] == "rework"),
        "runs": len(runs), "runs_by_outcome": by_outcome, "seconds_by_kind": by_kind,
        "started_at": started.isoformat() if started else None, "done_at": done_at.isoformat() if done_at else None,
        "done": done_at is not None,
    }
    for s in segs:
        s["start"], s["end"] = s["start"].isoformat(), s["end"].isoformat()
    return {"ticket": key, "status": t["status"], "generated_at": now.isoformat(), "segments": segs, "totals": totals}
