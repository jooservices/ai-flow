"""Watchdog check: store each account's quota forecast and open a prediction incident."""
from __future__ import annotations

from ..application_context import ApplicationContext
from ..db import J
from ..incidents import close_incident, open_incident
from ..quota import assessments as quota_assessments
from ..repositories.watchdog import QUOTA_FORECAST_ACCOUNTS, SAVE_QUOTA_FORECAST


def quota_forecasts(ctx: ApplicationContext) -> None:
    account_rows = ctx.db.all(QUOTA_FORECAST_ACCOUNTS)
    account_providers = {str(row["id"]): str(row["provider"]) for row in account_rows}
    for account_id, awareness in quota_assessments(ctx.db, account_rows).items():
        at_risk = [w for w in awareness["windows"]
                   if w["prediction"]["status"] == "will run out before reset"]
        ctx.db.run(SAVE_QUOTA_FORECAST,
                   (bool(at_risk), J(awareness), account_id))
        if at_risk:
            soonest = min((w["prediction"].get("exhaustion_at") or "") for w in at_risk)
            windows = ", ".join(w["label"] for w in at_risk)
            open_incident(ctx, "quota_prediction", account_id, "warning",
                          f"{account_id} may run out before reset",
                          f"Predicted exhaustion: {soonest or 'before reset'} UTC ({windows}). "
                          f"Confidence: {awareness['confidence']}. New work is routed to other "
                          f"{account_providers.get(account_id, 'AI')} accounts first.")
        else:
            close_incident(ctx, "quota_prediction", account_id, "quota forecast is no longer before reset")
