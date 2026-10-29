"""An owner question never blocks the flow for long (JAIF-370).

When the owner has not answered a question that carries a recommended option, and the question is not an irreversible or
production action, the recommendation is applied after the deadline (12 hours by default): the register entry becomes
decided by ``deadline``, the waiting tickets resume, and the choice stays visible so the owner can reverse it. A question
without a recommendation, or about an irreversible or production action, keeps waiting and opens one reminder incident.
"""
from __future__ import annotations

import logging

from . import escalations as esc
from .incidents import open_incident

log = logging.getLogger("aiflow4.escalation_deadline")

DEFAULT_DEADLINE_HOURS = 12
NOTE = "applied automatically after the deadline; the owner can reverse it"
OVERDUE_SQL = """
SELECT d.id, d.title, d.recommended, d.waiting_tickets,
       EXISTS (SELECT 1 FROM escalations e WHERE e.decision_id = d.id AND e.class = %(irreversible)s) AS irreversible
FROM owner_decisions d
WHERE d.kind = 'decision' AND d.status = 'asked' AND d.created_at < now() - make_interval(hours => %(hours)s)
ORDER BY d.id
"""


def overdue(db, hours: int = DEFAULT_DEADLINE_HOURS) -> list[dict]:
    return db.all(OVERDUE_SQL, {"hours": hours, "irreversible": esc.IRREVERSIBLE})


def apply_due(ctx, hours: int = DEFAULT_DEADLINE_HOURS) -> list[int]:
    """One watchdog pass: apply the recommendation of every overdue question that may be applied; returns their ids."""
    applied: list[int] = []
    for question in overdue(ctx.db, hours):
        if question["irreversible"] or not str(question["recommended"] or "").strip():
            why = "an irreversible or production action" if question["irreversible"] else "no recommended option"
            open_incident(ctx, "owner_question_overdue", f"D{question['id']}", "warning",
                          f"owner question D{question['id']} passed its {hours}-hour deadline",
                          f"{question['title']}: it waits for the owner because it is {why}.")
            continue
        try:
            esc.answer_decision(ctx, question["id"], "recommended", NOTE, "deadline")
        except LookupError:
            continue  # answered by the owner in the meantime
        except Exception as exc:  # noqa: BLE001 - one failing resume must not stop the others; the next pass retries
            log.warning("could not apply question D%s: %s", question["id"], exc)
            continue
        ctx.events.emit("escalation.deadline_applied", reason=question["title"], decision=question["id"])
        applied.append(question["id"])
    return applied
