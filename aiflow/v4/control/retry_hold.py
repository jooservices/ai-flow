"""Stop an unchanged retry loop: the third consecutive unsuccessful run of a ticket holds it (JAIF-367).

A streak is the number of consecutive unsuccessful runs (`failed`, `timeout`, `lost`, `no_changes`) of one ticket and
step over the whole history since the last release, plus the watchdog requeues of an orphaned In Progress ticket since
its last useful run. The third one adds the Jira label `aiflow-hold`, which work claims already honour, and leaves one
comment with the last three runs. Removing the label is the explicit release: the streak starts again from zero.
"""
from __future__ import annotations

import logging

log = logging.getLogger("aiflow4.retry_hold")

UNSUCCESSFUL = ("failed", "timeout", "lost", "no_changes")
THRESHOLD = 3
HOLD_LABEL = "aiflow-hold"
STEPS = ("dev", "verify")

_RELEASED = "coalesce((SELECT max(h.released_at) FROM retry_holds h WHERE h.ticket_key = {key}), '-infinity'::timestamptz)"
RUN_STREAK_SQL = f"""
WITH base AS (
  SELECT r.ticket_key, r.step, r.outcome,
         row_number() OVER (PARTITION BY r.ticket_key, r.step ORDER BY r.started_at DESC) AS n
  FROM runs r
  WHERE r.status <> 'running' AND r.step = ANY(%(steps)s) AND r.started_at > {_RELEASED.format(key="r.ticket_key")}
)
SELECT ticket_key, step,
       coalesce(min(n) FILTER (WHERE outcome <> ALL(%(bad)s)) - 1, count(*)) AS fails
FROM base GROUP BY ticket_key, step
"""
ORPHAN_SQL = f"""
SELECT w.ticket AS ticket_key, count(*) AS requeues FROM write_audit w
WHERE w.result = 'To Do (orphan)' AND w.ts > greatest(
  {_RELEASED.format(key="w.ticket")},
  coalesce((SELECT max(r.started_at) FROM runs r WHERE r.ticket_key = w.ticket AND r.status <> 'running'
            AND r.outcome <> ALL(%(bad)s)), '-infinity'::timestamptz))
GROUP BY w.ticket
"""


def streaks(db) -> dict[str, tuple[int, str, int]]:
    """Ticket key -> (streak length, step with the longest run streak, watchdog requeues in the streak)."""
    found: dict[str, tuple[int, str, int]] = {}
    for row in db.all(RUN_STREAK_SQL, {"steps": list(STEPS), "bad": list(UNSUCCESSFUL)}):
        if row["fails"] > found.get(row["ticket_key"], (0, "", 0))[0]:
            found[row["ticket_key"]] = (int(row["fails"]), row["step"], 0)
    for row in db.all(ORPHAN_SQL, {"bad": list(UNSUCCESSFUL)}):
        fails, step, _ = found.get(row["ticket_key"], (0, "dev", 0))
        found[row["ticket_key"]] = (fails + int(row["requeues"]), step, int(row["requeues"]))
    return found


def release_removed_holds(ctx) -> list[str]:
    """Close the hold of every ticket whose hold label was removed (the coordinator's explicit release)."""
    released = []
    for hold in ctx.db.all("SELECT h.id, h.ticket_key FROM retry_holds h LEFT JOIN tickets t ON t.key = h.ticket_key"
                           " WHERE h.released_at IS NULL AND t.key IS NOT NULL AND NOT (%s = ANY(t.labels))", (HOLD_LABEL,)):
        ctx.db.run("UPDATE retry_holds SET released_at = now() WHERE id = %s AND released_at IS NULL", (hold["id"],))
        ctx.events.emit("ticket.hold_released", ticket=hold["ticket_key"], reason="hold label removed")
        released.append(hold["ticket_key"])
    return released


def _last_runs(db, key: str, step: str) -> list[dict]:
    return db.all("SELECT id, node, account_id, outcome, error_kind, started_at, ended_at, summary FROM runs"
                  " WHERE ticket_key = %s AND step = %s AND status <> 'running'"
                  " AND started_at > " + _RELEASED.format(key="%s") + " ORDER BY started_at DESC LIMIT 3",
                  (key, step, key))


def comment_text(fails: int, step: str, runs: list[dict], requeues: int = 0) -> str:
    what = ", ".join(part for part in (f"{fails - requeues} unsuccessful {step} runs" if fails > requeues else "",
                                       f"{requeues} watchdog requeues" if requeues else "") if part)
    lines = [f"AI Flow · retry hold\nWhat: {what} in a row; the ticket is on hold ({HOLD_LABEL}).",
             "Why: another unchanged attempt would repeat the same result and only use worker time."]
    for run in runs:
        seconds = int((run["ended_at"] - run["started_at"]).total_seconds()) if run.get("ended_at") else 0
        lines.append(f"- run {run['id']}: {run['outcome']}{' / ' + run['error_kind'] if run['error_kind'] else ''},"
                     f" {run['node']} / {run['account_id']}, {seconds // 60} min. {str(run['summary'])[:160]}")
    lines.append(f"Next: the coordinator fixes the cause or splits the ticket, then removes the label {HOLD_LABEL} to restart it.")
    return "\n".join(lines)


def hold(ctx, key: str, fails: int, step: str, requeues: int = 0) -> bool:
    """Hold one ticket: label in Jira and in the registry, one comment, one hold row. False when it could not be held."""
    runs = _last_runs(ctx.db, key, step)
    try:
        ctx.jira.add_label(key, HOLD_LABEL)
    except Exception as exc:  # noqa: BLE001 - Jira can fail transiently; the next watchdog pass tries again
        log.warning("could not hold %s: %s", key, exc)
        return False
    # The label is the hold. From here the hold exists, so the row is recorded even when the comment cannot be written.
    if not record_hold(ctx, key, fails, step, [str(r["id"]) for r in runs]):
        return True  # another pass already recorded this hold and commented: no second comment or event
    try:
        ctx.jira.comment(key, comment_text(fails, step, runs, requeues))
    except Exception as exc:  # noqa: BLE001 - the comment is information; the hold does not depend on it
        log.warning("held %s but could not comment: %s", key, exc)
    ctx.events.emit("ticket.held", ticket=key, reason=f"{fails} unsuccessful {step} runs in a row")
    return True


def record_hold(ctx, key: str, fails: int, step: str, run_ids: list[str]) -> bool:
    """Remember a hold locally: the label in the registry and one active row, so releasing it resets the streak.

    True when this call inserted the row, False when the hold was already recorded."""
    ctx.db.run("UPDATE tickets SET labels = array_append(labels, %s) WHERE key = %s AND NOT (%s = ANY(labels))",
               (HOLD_LABEL, key, HOLD_LABEL))
    return bool(ctx.db.all("INSERT INTO retry_holds (ticket_key, step, fails, run_ids) VALUES (%s,%s,%s,%s)"
                           " ON CONFLICT DO NOTHING RETURNING id", (key, step, fails, run_ids)))


def apply(ctx) -> list[str]:
    """One watchdog pass: release removed holds, then hold every ticket that reached the threshold."""
    release_removed_holds(ctx)
    active = {r["ticket_key"] for r in ctx.db.all("SELECT ticket_key FROM retry_holds WHERE released_at IS NULL")}
    held = []
    for key, (fails, step, requeues) in streaks(ctx.db).items():
        if key in active:
            continue
        ticket = ctx.db.one("SELECT status, labels FROM tickets WHERE key = %s", (key,))
        if not ticket or ticket["status"] in ("Done", "Declined"):
            continue
        if HOLD_LABEL in (ticket["labels"] or []):
            # Held by the label but without an active row (a partial failure, or a hold put by a person): record it so
            # that removing the label resets the streak. No Jira write: the label is already there.
            record_hold(ctx, key, fails, step, [str(r["id"]) for r in _last_runs(ctx.db, key, step)])
            continue
        if fails >= THRESHOLD and hold(ctx, key, fails, step, requeues):
            held.append(key)
    return held
