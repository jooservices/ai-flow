"""Recording whether the master's action plan was valid, and the incident for repeated invalid plans."""
from ...incidents import close_incident, open_incident


def record_plan_outcome(ctx, run: dict, body, node: str) -> dict:
    inserted = ctx.db.one("INSERT INTO master_plan_cycles (run_id, valid) VALUES (%s,%s)"
                          " ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                          (body.run_id, body.valid))
    recorded = (ctx.db.one("SELECT valid FROM master_plan_cycles WHERE run_id=%s", (body.run_id,))
                if not inserted else {"valid": body.valid})
    valid = bool(recorded["valid"])
    if inserted:
        ctx.events.emit("master.plan", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="valid" if valid else "invalid",
                        reason="strict-format retry completed" if not valid else "valid action plan")
    streak, last_hour = invalid_plan_counts(ctx)
    incident_open = False
    if valid:
        close_incident(ctx, "master_invalid_plan", "global", "master returned a valid action plan")
    elif streak >= 3 or last_hour >= 5:
        incident_open = open_incident(
            ctx, "master_invalid_plan", "global", "critical", "master repeatedly returned an invalid plan",
            f"Invalid plan cycles: {streak} in a row and {last_hour} in the last hour. "
            "The master retried once with a strict JSON reminder on each cycle.")
    return {"valid": valid, "invalid_streak": streak, "invalid_last_hour": last_hour,
            "incident_opened": incident_open}


def invalid_plan_counts(ctx) -> tuple[int, int]:
    """(invalid cycles since the last valid one, invalid cycles in the last hour)."""
    streak_row = ctx.db.one(
        "SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid AND recorded_at > "
        "coalesce((SELECT max(recorded_at) FROM master_plan_cycles WHERE valid), 'epoch'::timestamptz)") or {}
    hour_row = ctx.db.one("SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid"
                          " AND recorded_at >= now()-interval '1 hour'") or {}
    return int(streak_row.get("n") or 0), int(hour_row.get("n") or 0)
