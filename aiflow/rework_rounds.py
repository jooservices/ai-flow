"""Rework round history for the loop limit (JAIF-93).

`cycles:<KEY>` counts the rounds a ticket returned to the developer; `rework-history:<KEY>` keeps the failing reason
of each counted round so the Blocked/Rework comment can list them all.
"""
import json


def feedback_summaries(items) -> str:
    """One line per feedback item (who, where, text) for the stored reason of a round."""
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = f"Feedback item {item.get('id', 'unknown')} from {item.get('who', 'unknown')} at {item.get('where', 'unknown')}"
        text = str(item.get("text", "")).strip()
        if text:
            line += f": {text[:800]}"
        lines.append(f"- {line}")
    return "Feedback item summaries:\n" + "\n".join(lines) if lines else ""


def failing_reason(headline: str, details: str, items) -> str:
    parts = [headline]
    if details.strip():
        parts.append(details.strip())
    summaries = feedback_summaries(items)
    if summaries:
        parts.append(summaries)
    return "\n".join(parts)[:8000]


def forget_block(redis, key: str) -> None:
    """The owner unblocked the ticket: drop the block and start counting rework rounds from zero (AC6)."""
    for name in ("blocked", "cycles", "rework-history"):
        redis.delete(f"{name}:{key}")


def record_round(redis, key: str, cycles: int, text: str, failure: tuple, loop_limit: int, ttl: int) -> str | None:
    """Store this round's rework `text` and failing reason (`failure` = headline, details, items); return the
    Blocked comment when `cycles` is over `loop_limit`.

    Rounds counted before history was kept are backfilled: only the newest earlier round has its text stored
    (the previous `rework:<KEY>`); older ones get a "not recorded" marker.
    """
    previous_text = str(redis.get(f"rework:{key}") or "")
    redis.set(f"rework:{key}", text[:12000], ttl)
    try:
        history = json.loads(redis.get(f"rework-history:{key}") or "[]")
    except (TypeError, ValueError):
        history = []
    history = [entry for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []
    recorded = {entry.get("round") for entry in history}
    for missing in range(1, cycles):
        if missing not in recorded:
            newest = missing == cycles - 1 and previous_text
            history.append({"round": missing, "reason": previous_text[:8000] if newest
                            else "not recorded (round counted before round history was kept)"})
    history.sort(key=lambda entry: entry.get("round", 0))
    history.append({"round": cycles, "reason": failing_reason(*failure)})
    redis.set(f"rework-history:{key}", json.dumps(history), ttl)
    if cycles <= loop_limit:
        return None
    reasons = "\n".join(f"- Round {entry['round']}: {entry['reason']}" for entry in history)
    return f"Rework loop limit of {loop_limit} exceeded at round {cycles}.\nFailing reason for each counted round:\n{reasons}"
