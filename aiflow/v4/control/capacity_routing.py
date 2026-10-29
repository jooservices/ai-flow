"""Select quota-safe development offers while respecting provider spend preference and hold-back."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException

from . import capacity, shared_accounts
from .repositories.accounts import AccountRepository


class CapacityRoutingService:
    """Route development candidates to the best-ranked provider with a safe quota forecast."""

    def __init__(self, ctx: Any, eligible_peer_candidates: Callable[..., list[dict[str, Any]]],
                 account_repository: AccountRepository, node_compatible: Callable[[str], bool]):
        self.ctx = ctx
        self.eligible_peer_candidates = eligible_peer_candidates
        self.account_repository = account_repository
        self.node_compatible = node_compatible

    def route(self, account_id: str, node: str, offered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        development = [ticket for ticket in offered if ticket.get("step") == "dev"]
        if not development:
            return offered
        account_rows = self.account_repository.capacity_routing_rows()
        if not account_rows:
            return offered
        accounts = {str(row["id"]): row for row in account_rows}
        candidates = self._eligible_by_ticket(account_id, node, offered, accounts)
        if not candidates:
            return offered
        selected = self._select_accounts(development, candidates)
        return self._keep_selected_offers(offered, account_id, selected)

    def _select_accounts(
        self,
        development: list[dict[str, Any]],
        candidates: dict[tuple[str, str], dict[str, dict[str, Any]]],
    ) -> dict[tuple[str, str], set[str]]:
        samples = capacity.history(self.ctx.db)
        routing = self.ctx.provider_eligibility.state(persist=False)
        selected: dict[tuple[str, str], set[str]] = {}
        for ticket in development:
            key = (ticket["key"], ticket["step"])
            peers = list(candidates.get(key, {}).values())
            accounts = self._select_ticket_accounts(ticket, peers, samples, routing)
            if accounts:
                selected[key] = accounts
        return selected

    def _select_ticket_accounts(self, ticket: dict[str, Any], peers: list[dict[str, Any]],
                                samples: list[dict[str, Any]], routing: dict[str, Any]) -> set[str] | None:
        if len(peers) < 2:
            return None
        estimate = capacity.estimate(ticket, samples)
        account = self._preferred_safe_account(peers, ticket, estimate, routing)
        if not account:
            return None
        labels = {str(label).lower() for label in ticket.get("labels") or []}
        labelled = {str(peer["id"]) for peer in peers if f"ai-{peer['provider']}" in labels}
        return {account} | labelled

    @staticmethod
    def _keep_selected_offers(offered: list[dict[str, Any]], account_id: str,
                              selected: dict[tuple[str, str], set[str]]) -> list[dict[str, Any]]:
        return [
            ticket for ticket in offered
            if CapacityRoutingService._is_selected_offer(ticket, account_id, selected)
        ]

    @staticmethod
    def _is_selected_offer(ticket: dict[str, Any], account_id: str,
                           selected: dict[tuple[str, str], set[str]]) -> bool:
        if ticket.get("step") != "dev":
            return True
        key = (ticket["key"], ticket["step"])
        if key not in selected:
            return True
        return account_id in selected[key]

    def _eligible_by_ticket(self, account_id: str, node: str, offered: list[dict[str, Any]],
                            accounts: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
        eligible: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        online = shared_accounts.online_nodes(self.ctx.db)
        for account in self.ctx.repo().accounts:
            row = accounts.get(account.id)
            if row is None:
                continue
            machines = [node] if account.id == account_id else account.machines
            for machine in dict.fromkeys(machines):
                if account.id != account_id and (machine not in online or not self.node_compatible(machine)):
                    continue
                for ticket in self._peer_offers(account.id, machine, account_id, node, offered):
                    if ticket.get("step") == "dev":
                        key = (ticket["key"], ticket["step"])
                        eligible.setdefault(key, {})[account.id] = row
        return eligible

    def _peer_offers(self, account: str, machine: str, account_id: str, node: str,
                     offered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if account == account_id and machine == node:
            return offered
        try:
            # The shared capacity router applies this same final choice during headroom peer probes.
            return self.eligible_peer_candidates(account, machine, capacity_routing=False)
        except HTTPException:
            return []

    @staticmethod
    def _preferred_safe_account(candidates: list[dict[str, Any]], ticket: dict[str, Any],
                                estimate: dict[str, Any], routing: dict[str, Any]) -> str | None:
        first, fits = capacity.choose_account(candidates, ticket, estimate,
                                              CapacityRoutingService._quota_forecasts(candidates))
        if not first:
            return None
        safe = CapacityRoutingService._capacity_safe_candidates(candidates, fits)
        provider = CapacityRoutingService._preferred_provider(safe, routing)
        preferred = CapacityRoutingService._provider_candidates(safe, provider)
        selected, _fits = capacity.choose_account(preferred, ticket, estimate,
                                                  CapacityRoutingService._quota_forecasts(preferred))
        return selected or first

    @staticmethod
    def _capacity_safe_candidates(candidates: list[dict[str, Any]],
                                  fits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in candidates
                if fits[str(row["id"])]["known"] and fits[str(row["id"])]["fits"]]

    @staticmethod
    def _preferred_provider(candidates: list[dict[str, Any]], routing: dict[str, Any]) -> str:
        return min({str(row["provider"]) for row in candidates},
                   key=lambda name: CapacityRoutingService._provider_priority(name, routing))

    @staticmethod
    def _provider_candidates(candidates: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
        return [row for row in candidates if row["provider"] == provider]

    @staticmethod
    def _quota_forecasts(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(row["id"]): row.get("quota_forecast") or {} for row in candidates}

    @staticmethod
    def _provider_priority(provider: str, routing: dict[str, Any]) -> tuple[bool, int, str]:
        state = (routing.get("providers") or {}).get(provider) or {}
        return bool(state.get("held_back")), int(state.get("rank", 1_000_000)), provider


def provider_aware_accounts(candidates: list[dict[str, Any]], ticket: dict[str, Any], estimate: dict[str, Any],
                            forecasts: dict[str, dict[str, Any]], routing: dict[str, Any]) -> set[str]:
    """The accounts that may take a ticket: the best quota-safe account of the preferred provider.

    Among the accounts whose quota forecast covers the ticket, the provider with the most headroom (the one that is
    not held back, then the best rank) wins; an account the ticket is labelled for (`ai-<provider>`) always stays.
    Empty when no account has a forecast that decides the choice."""
    first, fits = capacity.choose_account(candidates, ticket, estimate, forecasts)
    if not first:
        return set()
    safe = [row for row in candidates if fits[str(row["id"])]["known"] and fits[str(row["id"])]["fits"]]
    chosen = first
    if safe:
        provider = min({str(row["provider"]) for row in safe},
                       key=lambda name: CapacityRoutingService._provider_priority(name, routing))
        preferred = [row for row in safe if row["provider"] == provider]
        chosen = capacity.choose_account(preferred, ticket, estimate, forecasts)[0] or first
    labels = {str(label).lower() for label in ticket.get("labels") or []}
    return {chosen} | {str(peer["id"]) for peer in candidates if f"ai-{peer['provider']}" in labels}


def select_accounts(development: list[dict[str, Any]], eligible_by_key: dict[tuple[str, str], dict[str, dict[str, Any]]],
                    samples: list[dict[str, Any]], forecasts: dict[str, dict[str, Any]],
                    routing: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """Per development offer, the accounts allowed to take it (see provider_aware_accounts)."""
    selected: dict[tuple[str, str], set[str]] = {}
    for ticket in development:
        key = (ticket["key"], ticket["step"])
        candidates = list(eligible_by_key.get(key, {}).values())
        if len(candidates) < 2:
            continue
        accounts = provider_aware_accounts(candidates, ticket, capacity.estimate(ticket, samples), forecasts, routing)
        if accounts:
            selected[key] = accounts
    return selected
