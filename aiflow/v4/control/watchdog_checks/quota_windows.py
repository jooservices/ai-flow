"""Watchdog check: provider quota windows open and close quota_high incidents."""
from __future__ import annotations

from ...core.quota import as_utc
from ..application_context import ApplicationContext
from ..incidents import close_incident, open_incident
from ..repositories.watchdog import QUOTA_WINDOW_ACCOUNTS


def quota_windows(ctx: ApplicationContext) -> None:
    # quota windows reported by the provider (Claude: five_hour and seven_day utilization with reset times)
    for a in ctx.db.all(QUOTA_WINDOW_ACCOUNTS):
        for wname, w in (a["quota"].get("windows") or {}).items():
            u = w.get("utilization")
            if u is None:
                continue
            u = float(u)
            reset = w.get("resets_at")
            reset_at = as_utc(reset)
            reset_txt = reset_at.strftime("%Y-%m-%d %H:%M UTC") if reset_at else "unknown"
            label = {"five_hour": "5-hour", "seven_day": "weekly"}.get(wname, wname)
            subject = f"{a['id']}:{wname}"
            if u >= 0.9:
                sev = "critical" if u >= 1.0 else "warning"
                tip = (" If the account has a limit reset offer, Settings > Usage > Reset for free restores it now."
                       if a["provider"] == "claude" and wname == "seven_day" else "")
                open_incident(ctx, "quota_high", subject, sev, f"{a['id']} {label} limit at {round(u * 100)}%",
                              f"Resets at {reset_txt}. At 100% the account pauses until then and other accounts take"
                              f" its tickets.{tip}")
            elif u < 0.85:
                close_incident(ctx, "quota_high", subject, f"{label} usage back to {round(u * 100)}%")
