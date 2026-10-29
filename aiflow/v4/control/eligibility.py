"""Small work eligibility rules composed as a named control-plane pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable

from . import licensing, load_policy, provider_routing, rollout, shared_accounts
from .repositories.accounts import AccountRepository
from .repositories.provider_routing import ProviderRoutingRepository

log = logging.getLogger("aiflow4.eligibility")
PEER_OFFERS_TTL_S = 15.0
SNAPSHOT_TTL_S = 2.0


@dataclass(frozen=True)
class ProviderEligibilityContext:
    state: dict[str, Any]
    accounts: list[Any]
    online_nodes: set[str]
    ready_accounts: set[str]
    node_compatible: Callable[[str], bool]
    has_capacity: Callable[[Any, str], bool]
    eligible_tickets: Callable[[str, str], list[dict[str, Any]]]
    takeable: Callable[[str], set[tuple[str, str]]]  # offers a ready account of this provider can take now
    quota_forecast_at_risk: Callable[[str], bool]
    quota_peer_accounts: Callable[[str, set[str]], list[dict[str, Any]]]


class QuotaRiskRule:
    """Defer forecast-risky accounts while a ready same-provider peer can take their candidates."""

    name = "quota_forecast_risk"

    def filter(self, account: Any, tickets: list[dict[str, Any]],
               context: ProviderEligibilityContext) -> list[dict[str, Any]]:
        if not tickets or not context.quota_forecast_at_risk(account.id):
            return tickets
        roles = set(account.roles or ["dev", "verify"])
        peers = context.quota_peer_accounts(account.id, roles)
        elsewhere = self._peer_candidates(peers, context)
        return [ticket for ticket in tickets if (ticket["key"], ticket["step"]) not in elsewhere]

    @staticmethod
    def _peer_candidates(peers: list[dict[str, Any]], context: ProviderEligibilityContext) -> set[tuple[str, str]]:
        elsewhere: set[tuple[str, str]] = set()
        for peer in peers:
            for node in peer["nodes"]:
                if not context.node_compatible(node):
                    continue
                elsewhere.update((ticket["key"], ticket["step"])
                                 for ticket in context.eligible_tickets(peer["id"], node))
        return elsewhere


class ProviderHeadroomRule:
    """Defer offers when a ready account of a higher-ranked provider can take the same ticket."""

    name = "provider_headroom"

    def filter(self, account: Any, tickets: list[dict[str, Any]],
               context: ProviderEligibilityContext) -> list[dict[str, Any]]:
        state = context.state
        mine = state["providers"].get(account.provider)
        if not mine or len(state["ranking"]) < 2:
            return tickets
        ahead = self._providers_ahead(account.provider, mine, state)
        if not ahead:
            return tickets
        offers: dict[str, set[tuple[str, str]]] = {}
        return [ticket for ticket in tickets
                if self._keep_ticket(account, ticket, mine, ahead, context, offers)]

    @staticmethod
    def _keep_ticket(account: Any, ticket: dict[str, Any], mine: dict[str, Any], ahead: set[str],
                     context: ProviderEligibilityContext, offers: dict[str, set[tuple[str, str]]]) -> bool:
        labels = ticket.get("labels") or []
        if f"ai-{account.provider}" in labels or (mine["held_back"] and ticket["step"] == "triage"):
            return True
        key = (ticket["key"], ticket["step"])
        return not any(key in ProviderHeadroomRule._takeable(provider, context, offers) for provider in ahead)

    @staticmethod
    def _takeable(provider: str, context: ProviderEligibilityContext,
                  offers: dict[str, set[tuple[str, str]]]) -> set[tuple[str, str]]:
        if provider not in offers:
            offers[provider] = context.takeable(provider)
        return offers[provider]

    @staticmethod
    def _providers_ahead(provider: str, mine: dict[str, Any], state: dict[str, Any]) -> set[str]:
        return {name for name, other in state["providers"].items() if name != provider
                and ((not mine["held_back"] and other["rank"] < mine["rank"])
                     or (mine["held_back"] and (not other["held_back"] or other["rank"] < mine["rank"]))) }


class EligibilityPipeline:
    """Register named eligibility rules and apply them in registration order."""

    def __init__(self):
        self._rules: list[tuple[str, Any]] = []

    @property
    def rule_names(self) -> tuple[str, ...]:
        return tuple(name for name, _rule in self._rules)

    def register(self, rule: Any) -> None:
        name = str(getattr(rule, "name", "")).strip()
        if not name or any(existing == name for existing, _rule in self._rules):
            raise ValueError("eligibility rules must have unique names")
        if not callable(getattr(rule, "filter", None)):
            raise TypeError(f"eligibility rule {name!r} must define filter()")
        self._rules.append((name, rule))

    def filter(self, account: Any, tickets: list[dict[str, Any]],
               context: ProviderEligibilityContext) -> list[dict[str, Any]]:
        for _name, rule in self._rules:
            tickets = rule.filter(account, tickets, context)
        return tickets


class ProviderRoutingEligibilityService:
    """Build provider eligibility context and apply the registered headroom rule."""

    def __init__(self, ctx: Any, releases: Any, eligible_tickets: Callable[..., list[dict[str, Any]]],
                 quota_forecast_at_risk: Callable[[str], bool],
                 quota_peer_accounts: Callable[[str, set[str]], list[dict[str, Any]]]):
        self.ctx = ctx
        self.releases = releases
        self.repository = ProviderRoutingRepository(ctx.db)
        self.account_repository = AccountRepository(ctx.db)
        self.eligible = eligible_tickets  # the control app's eligible(); peers are probed through peer_candidates
        self.quota_forecast_at_risk = quota_forecast_at_risk
        self.quota_peer_accounts = quota_peer_accounts
        self._peer_lock = threading.Lock()
        self._peer_offers: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._snapshot_cache: dict[str, Any] | None = None
        self.pipeline = EligibilityPipeline()
        self.pipeline.register(QuotaRiskRule())
        self.pipeline.register(ProviderHeadroomRule())
        self.headroom_pipeline = EligibilityPipeline()  # eligible() already defers quota-risky accounts itself
        self.headroom_pipeline.register(ProviderHeadroomRule())

    def _shared_peer_offers(self, account: str, node: str) -> list[dict[str, Any]]:
        """A peer's offers, shared by the worker polls of the same second: headroom is advisory, and probing every
        peer on every poll made ticket pickup miss its budget at 100 projects, 50 machines and 200 sessions."""
        key, now = (account, node), time.monotonic()
        with self._peer_lock:
            cached = self._peer_offers.get(key)
            if cached and now - cached[0] < PEER_OFFERS_TTL_S:
                return cached[1]
        offers = self.peer_candidates(account, node, routing=False, capacity_routing=True)
        with self._peer_lock:
            self._peer_offers[key] = (now, offers)
        return offers

    def peer_candidates(self, account: str, node: str, *, routing: bool = False,
                        capacity_routing: bool = True) -> list[dict[str, Any]]:
        """Probe only work the account, machine and project plan allow it to claim."""
        try:
            state = licensing.enforce_work_entitlement(self.ctx, account, node,
                                                       public_key=self.ctx.settings.license_public_key)
        except licensing.LicenseError:
            return []
        allowed: list[dict[str, Any]] = []
        for ticket in self.eligible(account, node, routing=routing, capacity_routing=capacity_routing):
            try:
                licensing.enforce_project_entitlement(self.ctx, ticket["project"],
                                                      public_key=self.ctx.settings.license_public_key, state=state)
            except licensing.LicenseError:
                continue
            allowed.append(ticket)
        return allowed

    def state(self, persist: bool = True) -> dict[str, Any]:
        providers = sorted({account.provider for account in self.ctx.repo().accounts})
        accounts = self.account_repository.quota_routing_rows(providers)
        return provider_routing.evaluate(self.repository, providers, accounts, persist=persist)

    def node_compatible(self, node: str) -> bool:
        return rollout.node_compatible(self.ctx, node, self.releases)["compatible"]

    def filter(self, account: Any, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._run(self.pipeline, account, tickets)

    def filter_headroom(self, account: Any, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only the provider headroom rule: defer offers that a better-ranked provider can take."""
        return self._run(self.headroom_pipeline, account, tickets)

    def reset_cache(self) -> None:
        """Forget the shared snapshot and peer offers (the next poll reads the current state)."""
        with self._peer_lock:
            self._snapshot_cache = None
            self._peer_offers.clear()

    def _snapshot(self) -> dict[str, Any]:
        """The routing state, ready accounts, online machines and the per-machine answers shared by every worker poll
        of the same two seconds (the preference is advisory; a claim is checked again without it)."""
        now = time.monotonic()
        with self._peer_lock:
            cached = self._snapshot_cache
            if cached and now - cached["at"] < SNAPSHOT_TTL_S:
                return cached
        accounts = self.ctx.repo().accounts
        ready_accounts = self.account_repository.ready_ids()
        ready_accounts.intersection_update(account.id for account in accounts if not account.paused)
        snapshot = {"at": now, "accounts": accounts, "ready": ready_accounts, "state": self.state(),
                    "online": shared_accounts.online_nodes(self.ctx.db), "capacity": {}, "compatible": {},
                    "takeable": {}}
        with self._peer_lock:
            self._snapshot_cache = snapshot
        return snapshot

    def _takeable(self, snapshot: dict[str, Any], provider: str) -> set[tuple[str, str]]:
        """The (ticket, step) offers a ready account of `provider` can take now, on a machine that can work."""
        cached = snapshot["takeable"].get(provider)
        if cached is not None:
            return cached
        keys: set[tuple[str, str]] = set()
        for peer in snapshot["accounts"]:
            if peer.provider != provider or peer.id not in snapshot["ready"]:
                continue
            for machine in peer.machines:
                if (machine in snapshot["online"] and self._machine_compatible(snapshot, machine)
                        and self._machine_can_take(snapshot, peer, machine)):
                    keys.update((item["key"], item["step"]) for item in self._shared_peer_offers(peer.id, machine))
        snapshot["takeable"][provider] = keys
        return keys

    def _machine_can_take(self, snapshot: dict[str, Any], peer: Any, machine: str) -> bool:
        key = (peer.id, machine)
        if key not in snapshot["capacity"]:
            snapshot["capacity"][key] = bool(
                shared_accounts.may_take(self.ctx.db, peer, machine, as_peer=True)[0]
                and load_policy.claim_limits(self.ctx, machine)["allowed"])  # an overloaded machine offers no work
        return snapshot["capacity"][key]

    def _machine_compatible(self, snapshot: dict[str, Any], machine: str) -> bool:
        if machine not in snapshot["compatible"]:
            snapshot["compatible"][machine] = self.node_compatible(machine)
        return snapshot["compatible"][machine]

    def _run(self, pipeline: "EligibilityPipeline", account: Any,
             tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot = self._snapshot()
        context = ProviderEligibilityContext(
            state=snapshot["state"], accounts=snapshot["accounts"], online_nodes=snapshot["online"],
            ready_accounts=snapshot["ready"],
            node_compatible=lambda machine: self._machine_compatible(snapshot, machine),
            has_capacity=lambda peer, machine: self._machine_can_take(snapshot, peer, machine),
            eligible_tickets=self._shared_peer_offers,
            takeable=lambda provider: self._takeable(snapshot, provider),
            quota_forecast_at_risk=self.quota_forecast_at_risk,
            quota_peer_accounts=self.quota_peer_accounts)
        return pipeline.filter(account, tickets, context)

    def route(self, account_id: str, node: str, account: Any, tickets: list[dict[str, Any]], routing: bool,
              capacity_routing: bool, capacity_router: Callable[..., list[dict[str, Any]]]) -> list[dict[str, Any]]:
        if routing and tickets:
            tickets = self.filter(account, tickets)
        return capacity_router(account_id, node, tickets) if capacity_routing and tickets else tickets

    def claim_run(self, account: Any, values: tuple, ticket: dict[str, Any], step: str) -> bool:
        if not shared_accounts.claim_run(self.ctx.db, account, values):
            return False
        self.record_assignment(ticket, account, step)
        return True

    def record_assignment(self, ticket: dict[str, Any], account: Any, step: str) -> None:
        if step == "master":
            return
        try:
            kind, reason = provider_routing.classify(self.state(persist=False), account.provider, step,
                                                     ticket.get("labels") or [])
            provider_routing.record(self.repository, ticket["key"], step, account.id, account.provider, kind, reason)
        except Exception as exc:  # noqa: BLE001 - a missing explanation must never block a claimed ticket
            log.warning("routing decision for %s not recorded: %s", ticket.get("key", ""), type(exc).__name__)


def install(ctx: Any, releases: Any, eligible: Callable[..., list[dict[str, Any]]],
            quota_forecast_at_risk: Callable[[str], bool],
            quota_peer_accounts: Callable[[str, set[str]], list[dict[str, Any]]]) -> None:
    """Give the control context its provider routing services (docs/v4/provider-routing.md)."""
    ctx.provider_routing = provider_routing.ProviderRoutingService(ctx)
    ctx.provider_eligibility = ProviderRoutingEligibilityService(ctx, releases, eligible, quota_forecast_at_risk,
                                                                 quota_peer_accounts)
