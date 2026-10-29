"""Quota-based provider preferences, ceilings and assignment explanations (docs/v4/provider-routing.md)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.quota import as_utc
from .repositories.accounts import AccountRepository
from .repositories.provider_routing import ProviderRoutingRepository

DEFAULT_ORDER = ("codex", "grok", "claude", "opencode")
DEFAULT_CEILING = 85
RETURN_BELOW = 60          # a held-back provider returns when every window is below this share
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


def get_settings(repository: ProviderRoutingRepository) -> dict[str, Any]:
    row = repository.settings_row()
    order = list((row or {}).get("provider_order") or [])
    ceilings = {str(k): int(v) for k, v in ((row or {}).get("ceilings") or {}).items()}
    return {"order": order, "ceilings": ceilings, "updated_by": (row or {}).get("updated_by", ""),
            "updated_at": row["updated_at"].isoformat() if row else None,
            "defaults": {"order": list(DEFAULT_ORDER), "ceiling": DEFAULT_CEILING}}


def _merge_settings(previous: dict[str, Any], order: list[str] | None,
                    ceilings: dict[str, int] | None) -> tuple[list[str], dict[str, int]]:
    saved_order = previous["order"] if order is None else order
    saved_ceilings = dict(previous["ceilings"])
    if ceilings is not None:
        saved_ceilings = dict(ceilings) if not ceilings else {**saved_ceilings, **ceilings}
    return saved_order, saved_ceilings


def set_settings(repository: ProviderRoutingRepository, order: list[str] | None, ceilings: dict[str, int] | None,
                 by: str) -> dict[str, Any]:
    previous = get_settings(repository)
    order, requested_ceilings = _merge_settings(previous, order, ceilings)
    clean_order: list[str] = []
    for provider in order:
        provider = str(provider).strip().lower()
        if not provider or len(provider) > 40:
            raise ValueError("a provider name is empty or too long")
        if provider not in clean_order:
            clean_order.append(provider)
    clean: dict[str, int] = {}
    for provider, value in requested_ceilings.items():
        try:
            percent = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ceiling for {provider} must be a whole number") from exc
        if not 1 <= percent <= 100:
            raise ValueError(f"ceiling for {provider} must be between 1 and 100 percent")
        normalized_provider = str(provider).strip().lower()
        if not normalized_provider or len(normalized_provider) > 40:
            raise ValueError("a provider name is empty or too long")
        clean[normalized_provider] = percent
    changed_ceilings = sorted(provider for provider in set(previous["ceilings"]) | set(clean)
                              if int(previous["ceilings"].get(provider, DEFAULT_CEILING))
                              != int(clean.get(provider, DEFAULT_CEILING)))
    repository.save_settings(clean_order, clean, by)
    # A changed ceiling is judged against current usage; unrelated setting changes keep hysteresis intact.
    repository.clear_state(changed_ceilings)
    return get_settings(repository)


def resolve_order(saved: list[str], providers: list[str]) -> list[str]:
    """The owner's order first, then the default order, then any other provider by name."""
    known = set(providers)
    out = [p for p in saved if p in known]
    out += [p for p in DEFAULT_ORDER if p in known and p not in out]
    out += sorted(p for p in known if p not in out)
    return out


def _account_window(account: dict, name: str, window: Any, now: datetime) -> dict | None:
    used = _used_pct(window)
    if used is None:
        return None
    reset = as_utc(window.get("resets_at") or window.get("reset_at"))
    expired = bool(reset and reset <= now)
    return {"name": str(name), "account": str(account.get("id") or ""),
            "used_pct": 0.0 if expired else used,
            "reset_at": reset.isoformat() if reset else None, "reset": expired}


def provider_windows(accounts: list[dict], now: datetime) -> list[dict]:
    """Return each account's live window separately so one account cannot mask another's quota use."""
    out = []
    for account in accounts:
        for name, window in (account.get("quota") or {}).get("windows", {}).items():
            item = _account_window(account, name, window, now)
            if item:
                out.append(item)
    return sorted(out, key=lambda window: (window["name"], window["account"], window["reset_at"] or ""))


def _preference_score(headroom: float, windows: list[dict], at_risk_share: float, now: datetime) -> float:
    score = headroom - 15 * at_risk_share
    for window in windows:
        reset = as_utc(window.get("reset_at"))
        if reset and now < reset <= now + timedelta(hours=1) and 100 - window["used_pct"] >= 20:
            score += 10
            break
    return score


def _previous_hold(held_row: dict | None, counted: list[dict], release_below: int,
                   now: datetime) -> tuple[bool, str]:
    if not held_row:
        return False, ""
    reset_at = as_utc(held_row.get("window_reset_at"))
    if reset_at and now >= reset_at:
        return False, ""
    reset_note = f" at {reset_at:%H:%M} UTC" if reset_at else ""
    if not counted:  # a transient or partial quota response carries no window: keep the hold until its window resets
        return True, (f"held until the {held_row['window_name'].replace('_', ' ')} window resets{reset_note}; "
                      "no quota window is reported right now")
    if all(window["used_pct"] < release_below for window in counted):
        return False, ""
    busiest = max(counted, key=lambda window: window["used_pct"])
    reason = (f"held until the {held_row['window_name'].replace('_', ' ')} window resets{reset_note} or usage is "
              f"below {release_below}% (now {busiest['used_pct']:g}%)")
    return True, reason


def _hold_status(ceiling: int, windows: list[dict], held_row: dict | None,
                 now: datetime) -> tuple[bool, str, dict | None]:
    counted = [window for window in windows if not window["reset"]]
    over = [window for window in counted if window["used_pct"] > ceiling]
    if over:
        trigger = max(over, key=lambda window: window["used_pct"])
        account = f" for {trigger['account']}" if trigger.get("account") else ""
        reason = (f"{trigger['name'].replace('_', ' ')} window{account} at {trigger['used_pct']:g}%, "
                  f"above its {ceiling}% ceiling")
        return True, reason, trigger
    held, reason = _previous_hold(held_row, counted, min(RETURN_BELOW, ceiling), now)
    return held, reason, None


def _persist_hold_state(repository: ProviderRoutingRepository, provider: str, held: bool,
                        trigger: dict | None, held_row: dict | None) -> None:
    if held and trigger:
        repository.hold_provider(provider, trigger["name"], as_utc(trigger.get("reset_at")), trigger["used_pct"])
    elif not held and held_row:
        repository.release_provider(provider)


def _provider_state(repository: ProviderRoutingRepository, provider: str, mine: list[dict],
                    settings: dict[str, Any], held_row: dict | None, now: datetime,
                    persist: bool) -> tuple[dict, float]:
    windows = provider_windows(mine, now)
    ceiling = int(settings["ceilings"].get(provider, DEFAULT_CEILING))
    headroom = 100.0 if not windows else min(100 - window["used_pct"] for window in windows)
    held, reason, trigger = _hold_status(ceiling, windows, held_row, now)
    if persist:
        _persist_hold_state(repository, provider, held, trigger, held_row)
    at_risk = sum(1 for row in mine if row["quota_at_risk"]) / len(mine) if mine else 0.0
    score = _preference_score(headroom, [window for window in windows if not window["reset"]], at_risk, now)
    return ({"provider": provider, "accounts": len(mine), "ceiling_pct": ceiling, "windows": windows,
             "headroom_pct": round(headroom, 1), "unknown": not windows, "held_back": held,
             "held_reason": reason, "at_risk_share": round(at_risk, 2), "score": round(score, 1)}, score)


def _apply_preferences(states: dict[str, dict], ranking: list[str]) -> None:
    for index, provider in enumerate(ranking):
        state = states[provider]
        if state["held_back"]:
            state["preference"] = "held back"
        elif len(ranking) > 1 and index == 0:
            state["preference"] = "spend first"
        elif len(ranking) > 1 and index == len(ranking) - 1:
            state["preference"] = "keep for need"
        else:
            state["preference"] = "normal"
        state["note"] = ("Quota unknown: the provider reports no quota, so it is treated as having headroom."
                         if state["unknown"] else "")


def evaluate(repository: ProviderRoutingRepository, providers: list[str], account_rows: list[dict[str, Any]],
             now: datetime | None = None, persist: bool = True) -> dict[str, Any]:
    """The routing state of every provider in `providers`, updating the hold-back state when `persist`."""
    now = as_utc(now) or datetime.now(timezone.utc)
    settings = get_settings(repository)
    order = resolve_order(settings["order"], providers)
    held_rows = repository.routing_states()
    states: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for provider in providers:
        mine = [row for row in account_rows if row["provider"] == provider]
        states[provider], scores[provider] = _provider_state(
            repository, provider, mine, settings, held_rows.get(provider), now, persist)
    ranking = sorted(providers, key=lambda p: (states[p]["held_back"], -scores[p], order.index(p)))
    for index, provider in enumerate(ranking):
        states[provider]["rank"] = index
    _apply_preferences(states, ranking)
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


def record(repository: ProviderRoutingRepository, ticket: str, step: str, account: str,
           provider: str, kind: str, reason: str) -> None:
    repository.record_decision(ticket, step, account, provider, kind, reason)


def recent_decisions(repository: ProviderRoutingRepository, limit: int = 30, offset: int = 0,
                     ticket: str = "") -> list[dict]:
    return repository.decisions(limit, offset, ticket)


def count_decisions(repository: ProviderRoutingRepository, ticket: str = "") -> int:
    return repository.decision_count(ticket)


class ProviderRoutingService:
    """Orchestrate provider state, owner settings and assignment history for the console routes."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.repository = ProviderRoutingRepository(ctx.db)
        self.account_repository = AccountRepository(ctx.db)

    def state(self) -> dict[str, Any]:
        providers = sorted({account.provider for account in self.ctx.repo().accounts})
        accounts = self.account_repository.quota_routing_rows(providers)
        return evaluate(self.repository, providers, accounts)

    def view(self) -> dict[str, Any]:
        state = self.state()
        return {"order": state["order"],
                "providers": [state["providers"][name] for name in state["ranking"]],
                "settings": state["settings"],
                "decisions": recent_decisions(self.repository)}

    def decisions(self, limit: int, offset: int, ticket: str = "") -> dict[str, Any]:
        ticket = ticket.strip().upper()
        limit, offset = max(1, min(limit, 200)), max(0, offset)
        return {"decisions": recent_decisions(self.repository, limit, offset, ticket),
                "total": count_decisions(self.repository, ticket),
                "limit": limit, "offset": offset, "ticket": ticket}

    def save_settings(self, order: list[str] | None, ceilings: dict[str, int] | None,
                      by: str) -> dict[str, Any]:
        known = {account.provider for account in self.ctx.repo().accounts}
        previous = get_settings(self.repository)
        effective_order, effective_ceilings = _merge_settings(previous, order, ceilings)
        unknown = sorted((set(effective_order) | set(effective_ceilings)) - known)
        if unknown:
            raise ValueError(f"unknown provider: {', '.join(unknown)}")
        set_settings(self.repository, order, ceilings, by)
        return self.view()
