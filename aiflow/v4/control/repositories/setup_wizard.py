"""Persistence operations for first-run setup progress."""
from __future__ import annotations

from ..db import J


def set_step_skipped(db, step_id: str, skipped: bool, actor: str) -> None:
    """Persist a skip or resume and its audit row as one database transaction."""
    action = "skip_step" if skipped else "resume_step"
    result = "setup step skipped" if skipped else "setup step resumed"
    with db.pool.connection() as connection:
        with connection.transaction():
            if skipped:
                connection.execute(
                    "UPDATE setup_wizard_state SET skipped_steps=array_append(array_remove(skipped_steps,%s),%s),"
                    "updated_at=now() WHERE id=true", (step_id, step_id))
            else:
                connection.execute("UPDATE setup_wizard_state SET skipped_steps=array_remove(skipped_steps,%s),"
                                   "updated_at=now() WHERE id=true", (step_id,))
            connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                               " VALUES ('setup',%s,'',%s,true,%s,%s)",
                               (action, actor, result, J({"step": step_id})))
