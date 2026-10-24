"""Spend the provider with plenty of quota first and hold back the one that runs out fast (docs/v4/provider-routing.md).

Accounts poll the control API for work, so routing is a filter in `eligible()` (api.py): an account is not offered a
ticket while an account of a more preferred provider can take it. This module decides the provider order, which
providers are held back, and records why each ticket went to the account that claimed it.

* Headroom: for every window a provider reports (five hour, seven day, ...) the usage of its accounts is averaged;
  the provider's headroom is what is left in its fullest window. A window whose own reset time has passed counts as
  reset. A provider that reports no quota windows has *unknown* quota and is treated as having full headroom.
* Spend preference (JAIF-245 data): headroom, minus up to 15 points when its accounts are forecast to run out before
  reset, plus 10 points when a window with 20% or more left resets within the hour (spend it before it renews).
  Headroom is compared in 10-point bands so the owner's order (default Codex, Grok, Claude, OpenCode) breaks near ties.
* Ceiling: a provider above its ceiling (default 85%) in any window is held back. It returns to normal use when the
  held window resets, or when every window is below 60% (or below its ceiling, if that is lower).
* A held-back provider is offered only master and triage work, tickets labelled `ai-<provider>`, and work no other
  provider can do. Its accounts stay ready, so the role coverage check in loops.py is not affected.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.quota import as_utc

DEFAULT_ORDER = ("codex", "grok", "claude", "opencode")
DEFAULT_CEILING = 85
RETURN_BELOW = 60          # a held-back provider returns when every window is below this share
BAND = 10                  # headroom points that count as "otherwise equal"
NEEDED_STEPS = {"triage"}  # the master cycle is claimed through its own candidate and is never deferred


def _used_pct(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    raw = window.get("utilization", window.get("used_percent"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    # same reading as core/quota.py: up to 1 is a share, above 1 is a percentage
    return max(0.0, min(100.0, value if value > 1 else value * 100))


def get_settings(db) -> dict[str, Any]:
    row = db.one("SELECT provider_order, ceilings, updated_by, updated_at FROM provider_routing_settings")
    order = list((row or {}).get("provider_order") or [])
    ceilings = {str(k): int(v) for k, v in ((row or {}).get("ceilings") or {}).items()}
    return {"order": order, "ceilings": ceilings, "updated_by": (row or {}).get("updated_by", ""),
            "updated_at": row["updated_at"].isoformat() if row else None,
            "defaults": {"order": list(DEFAULT_ORDER), "ceiling": DEFAULT_CEILING}}


def set_settings(db, order: list[str], ceilings: dict[str, int], by: str) -> dict[str, Any]:
    clean_order: list[str] = []
    for provider in order:
        provider = str(provider).strip().lower()
        if not provider or len(provider) > 40:
            raise ValueError("a provider name is empty or too long")
        if provider not in clean_order:
            clean_order.append(provider)
    clean: dict[str, int] = {}
    for provider, value in ceilings.items():
        try:
            percent = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ceiling for {provider} must be a whole number") from exc
        if not 1 <= percent <= 100:
            raise ValueError(f"ceiling for {provider} must be between 1 and 100 percent")
        clean[str(provider).strip().lower()] = percent
    db.run("INSERT INTO provider_routing_settings (id, provider_order, ceilings, updated_by)"
           " VALUES (true,%s,%s::jsonb,%s) ON CONFLICT (id) DO UPDATE SET provider_order=EXCLUDED.provider_order,"
           " ceilings=EXCLUDED.ceilings, updated_by=EXCLUDED.updated_by, updated_at=now()",
           (clean_order, json.dumps(clean), by[:200]))
    # a new ceiling is judged on the current usage; the old hold-back state belongs to the old ceiling
    db.run("DELETE FROM provider_routing_state")
    return get_settings(db)


def resolve_order(saved: list[str], providers: list[str]) -> list[str]:
    """The owner's order first, then the default order, then any other provider by name."""
    known = set(providers)
    out = [p for p in saved if p in known]
    out += [p for p in DEFAULT_ORDER if p in known and p not in out]
    out += sorted(p for p in known if p not in out)
    return out


def provider_windows(quotas: list[dict], now: datetime) -> list[dict]:
    """Per window name: mean usage of the accounts that report it, and the window's own reset time."""
    grouped: dict[str, list[tuple[float, datetime | None]]] = {}
    for quota in quotas:
        for name, window in ((quota or {}).get("windows") or {}).items():
            used = _used_pct(window)
            if used is None:
                continue
            reset = as_utc(window.get("resets_at") or window.get("reset_at"))
            grouped.setdefault(str(name), []).append((used, reset))
    out = []
    for name, points in sorted(grouped.items()):
        live = [(u, r) for u, r in points if r is None or r > now]
        resets = [r for _, r in live if r]
        if live:
            out.append({"name": name, "used_pct": round(sum(u for u, _ in live) / len(live), 1),
                        "reset_at": min(resets).isoformat() if resets else None, "reset": False})
        else:  # every report is from a window that has since reset
            out.append({"name": name, "used_pct": 0.0, "reset_at": None, "reset": True})
    return out


def _preference_score(headroom: float, windows: list[dict], at_risk_share: float, now: datetime) -> float:
    score = headroom - 15 * at_risk_share
    for window in windows:
        reset = as_utc(window.get("reset_at"))
        if reset and now < reset <= now + timedelta(hours=1) and 100 - window["used_pct"] >= 20:
            score += 10
            break
    return score


def evaluate(db, providers: list[str], now: datetime | None = None, persist: bool = True) -> dict[str, Any]:
    """The routing state of every provider in `providers`, updating the hold-back state when `persist`."""
    now = as_utc(now) or datetime.now(timezone.utc)
    settings = get_settings(db)
    order = resolve_order(settings["order"], providers)
    rows = db.all("SELECT provider, quota, quota_at_risk FROM accounts WHERE provider=ANY(%s)", (providers,))
    held_rows = {r["provider"]: r for r in db.all("SELECT * FROM provider_routing_state")}
    states: dict[str, dict] = {}
    for provider in providers:
        mine = [r for r in rows if r["provider"] == provider]
        windows = provider_windows([r["quota"] or {} for r in mine], now)
        ceiling = int(settings["ceilings"].get(provider, DEFAULT_CEILING))
        counted = [w for w in windows if not w["reset"]]
        headroom = 100.0 if not windows else (min(100 - w["used_pct"] for w in windows))
        over = [w for w in counted if w["used_pct"] >= ceiling]
        held_row = held_rows.get(provider)
        held, reason, trigger = False, "", None
        release_below = min(RETURN_BELOW, ceiling)
        if over:
            trigger = max(over, key=lambda w: w["used_pct"])
            held = True
            reason = (f"{trigger['name'].replace('_', ' ')} window at {trigger['used_pct']:g}%, "
                      f"above its {ceiling}% ceiling")
        elif held_row:
            reset_at = as_utc(held_row.get("window_reset_at"))
            window_reset = bool(reset_at and now >= reset_at)
            low = all(w["used_pct"] < release_below for w in counted)
            if not window_reset and not low:
                held = True
                busiest = max(counted, key=lambda w: w["used_pct"])
                reason = (f"held until the {held_row['window_name'].replace('_', ' ')} window resets"
                          f"{f' at {reset_at:%H:%M} UTC' if reset_at else ''} or usage is below {release_below}%"
                          f" (now {busiest['used_pct']:g}%)")
        if persist:
            if held and trigger:
                db.run("INSERT INTO provider_routing_state (provider, window_name, window_reset_at, used_pct)"
                       " VALUES (%s,%s,%s,%s) ON CONFLICT (provider) DO UPDATE SET window_name=EXCLUDED.window_name,"
                       " window_reset_at=EXCLUDED.window_reset_at, used_pct=EXCLUDED.used_pct",
                       (provider, trigger["name"], as_utc(trigger.get("reset_at")), trigger["used_pct"]))
            elif not held and held_row:
                db.run("DELETE FROM provider_routing_state WHERE provider=%s", (provider,))
        at_risk = sum(1 for r in mine if r["quota_at_risk"]) / len(mine) if mine else 0.0
        states[provider] = {
            "provider": provider, "accounts": len(mine), "ceiling_pct": ceiling, "windows": windows,
            "headroom_pct": round(headroom, 1), "unknown": not windows, "held_back": held, "held_reason": reason,
            "at_risk_share": round(at_risk, 2),
            "score": round(_preference_score(headroom, counted, at_risk, now), 1)}
    ranking = sorted(providers, key=lambda p: (states[p]["held_back"], -(states[p]["score"] // BAND),
                                                order.index(p)))
    for index, provider in enumerate(ranking):
        state = states[provider]
        state["rank"] = index
        if state["held_back"]:
            state["preference"] = "held back"
        elif len(ranking) > 1 and index == 0:
            state["preference"] = "spend first"
        elif len(ranking) > 1 and index == len(ranking) - 1:
            state["preference"] = "keep for need"
        else:
            state["preference"] = "normal"
        if state["unknown"]:
            state["note"] = "Quota unknown: the provider reports no quota, so it is treated as having headroom."
        else:
            state["note"] = ""
    return {"order": order, "ranking": ranking, "providers": states, "settings": settings}


def classify(state: dict[str, Any], provider: str, step: str, labels: list[str]) -> tuple[str, str]:
    """(kind, reason) for a ticket that was offered to and claimed by an account of `provider`."""
    mine = state["providers"].get(provider) or {}
    if f"ai-{provider}" in labels:
        return "label", f"labelled ai-{provider}, so it goes to {provider} whatever its headroom"
    if mine.get("held_back"):
        if step in NEEDED_STEPS:
            return "needed", f"{provider} is held back ({mine['held_reason']}) but {step} needs it"
        return "fallback", f"{provider} is held back ({mine['held_reason']}); no other provider could take it"
    if len(state["ranking"]) < 2:
        return "only", f"{provider} is the only provider"
    if mine.get("rank", 0) == 0:
        extra = "unknown quota treated as headroom" if mine.get("unknown") else f"{mine['headroom_pct']:g}% headroom"
        return "headroom", f"headroom order: {provider} first ({extra})"
    first = next(p for p in state["ranking"] if p != provider)
    return "fallback", f"fallback: no {first} account could take it, so {provider} took it"


def record(db, ticket: str, step: str, account: str, provider: str, kind: str, reason: str) -> None:
    db.run("INSERT INTO routing_decisions (ticket_key, step, account_id, provider, kind, reason)"
           " VALUES (%s,%s,%s,%s,%s,%s)", (ticket, step, account, provider, kind, reason[:500]))


def recent_decisions(db, limit: int = 30, offset: int = 0, ticket: str = "") -> list[dict]:
    where, args = ("WHERE ticket_key=%s", (ticket,)) if ticket else ("", ())
    return [{"ticket": r["ticket_key"], "step": r["step"], "account": r["account_id"], "provider": r["provider"],
             "kind": r["kind"], "reason": r["reason"], "at": r["at"].isoformat()}
            for r in db.all(f"SELECT * FROM routing_decisions {where} ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
                            args + (limit, offset))]


def count_decisions(db, ticket: str = "") -> int:
    where, args = ("WHERE ticket_key=%s", (ticket,)) if ticket else ("", ())
    return int(db.one(f"SELECT count(*) AS n FROM routing_decisions {where}", args)["n"])
