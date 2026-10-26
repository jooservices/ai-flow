"""Shared composition of normal candidate eligibility for API and watchdog callers."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import HTTPException

from . import capacity, licensing, role_health, rollout, shared_accounts, verify_retry, work_candidates
from . import work_eligibility
from .repositories.accounts import AccountsRepository
from .repositories.nodes import NodesRepository
from .repositories.runs import RunsRepository
from .repositories.tickets import TicketsRepository
from ..core.controlrepo import Account
from ..core.toolchains import CONTAINER_RUNTIME_TOOL, toolchain_gaps


class CandidateRuntime:
    """The complete work-offer path, usable without constructing a FastAPI application."""

    def __init__(self, ctx, releases: Path) -> None:
        self.ctx = ctx
        self.releases = releases

    def quota_forecast_at_risk(self, account_id: str) -> bool:
        return AccountsRepository(self.ctx.db).quota_at_risk(account_id)

    def quota_peer_accounts(self, account_id: str, roles: set[str] | None = None) -> list[dict]:
        ctx = self.ctx
        peers = AccountsRepository(ctx.db).quota_peer_candidates(account_id)
        online = shared_accounts.online_nodes(ctx.db)
        available = []
        for peer in peers:
            account = ctx.repo().account(peer["id"])
            peer["nodes"] = ([machine for machine in account.machines if machine in online
                              and shared_accounts.may_take(ctx.db, account, machine, as_peer=True)[0]]
                             if account else [])
            if not peer["nodes"]:
                continue
            peer_roles = set(peer.get("roles") or ["dev", "verify"])
            if roles and not roles.intersection(peer_roles):
                continue
            if roles == {"master"} and peer["status"] not in ("idle", "working", "probing"):
                continue
            if peer["quota_at_risk"] or ctx.leases.cooling(peer["id"]):
                continue
            if work_eligibility.master_paused(ctx.db, peer["id"]):
                continue
            available.append(peer)
        return available

    def account_quota_at_risk(self, account_id: str, roles: set[str] | None = None) -> bool:
        return self.quota_forecast_at_risk(account_id) and bool(self.quota_peer_accounts(account_id, roles))

    def eligible(self, account_id: str, node: str, *, capacity_routing: bool = True,
                 verify_retry_guard: bool = True) -> list[dict]:
        ctx = self.ctx
        repo, account = work_candidates.resolve_account(ctx, account_id, node)
        if not work_eligibility.can_take_work(ctx.db, ctx.leases, account, node):
            return []
        node_row = NodesRepository(ctx.db).candidate_node(node)
        if not node_row.get("accepting_work", True):
            return []
        projects = work_candidates.available_projects(ctx, repo, node, node_row)
        if not projects:
            return []
        roles = set(account.roles or ["dev", "verify"])
        state = work_candidates.CandidateContext(
            ctx=ctx, repo=repo, account=account, node=node, node_row=node_row, projects=projects,
            missing_container_runtime=False, roles=roles,
            modes=role_health.modes(ctx.db, account.id, sorted(roles)),
            held=set(ctx.leases.held_keys()),
            priorities={key: int(getattr(project, "priority", 5) or 5)
                        for key, project in repo.projects.items()},
            quota_at_risk=self.quota_forecast_at_risk(account_id), capacity_routing=capacity_routing,
            verify_retry_guard=verify_retry_guard, runs_repo=RunsRepository(ctx.db),
            tickets_repo=TicketsRepository(ctx.db), quota_peers=self.quota_peer_accounts,
            peer_compatible=rollout.node_compatible,
            peer_offers=lambda peer, machine, guard: self.eligible(
                peer, machine, capacity_routing=False, verify_retry_guard=guard),
            capacity_route=self.route_capacity, entitled_candidates=self.entitled_candidates,
            releases=self.releases, license_error=licensing.LicenseError)
        state.missing_container_runtime = bool(
            toolchain_gaps(node_row.get("tools") or {}, [CONTAINER_RUNTIME_TOOL], repo.versions))
        return work_candidates.collect(state)

    def route_capacity(self, account_id: str, node: str, offered: list[dict],
                       verify_retry_guard: bool = True) -> list[dict]:
        return capacity.route_candidates(self.ctx, account_id, node, offered, self.eligible,
                                         shared_accounts.online_nodes, verify_retry_guard)

    def entitled_candidates(self, account_id: str, node: str, *, verify_retry_guard: bool = True
                            ) -> tuple[list[dict], str]:
        ctx = self.ctx
        state = licensing.enforce_work_entitlement(ctx, account_id, node,
                                                    public_key=ctx.settings.license_public_key)
        candidates = self.eligible(account_id, node, verify_retry_guard=verify_retry_guard)
        allowed, refused = [], []
        for ticket in candidates:
            try:
                licensing.enforce_project_entitlement(ctx, ticket["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                refused.append(str(exc))
            else:
                allowed.append(ticket)
        reason = refused[0] if candidates and not allowed and refused else ""
        return allowed, reason

    def final_candidate_offers(self, account_id: str, node: str) -> list[dict]:
        ctx = self.ctx
        if node not in shared_accounts.online_nodes(ctx.db):
            return []
        return verify_retry.final_offers(ctx, account_id, node, self.entitled_candidates,
                                         rollout.node_compatible, self.releases, licensing.LicenseError)

    def quota_peer_release_candidates(self, account_id: str) -> set[tuple[str, str, str]]:
        if not self.quota_forecast_at_risk(account_id):
            return set()
        from . import version_release
        available = set()
        for peer in self.quota_peer_accounts(account_id, {"master", "release"}):
            for node in peer["nodes"]:
                if not rollout.node_compatible(self.ctx, node, self.releases)["compatible"]:
                    continue
                for candidate in version_release.candidates(self.ctx, peer["id"], node):
                    available.add((candidate["project"], str(candidate["version_id"]),
                                   str(candidate.get("repository") or "")))
        return available


def initialize(ctx, releases: Path) -> CandidateRuntime:
    """Install the shared account view and candidate probe once per control context."""
    runtime = getattr(ctx, "_candidate_runtime", None)
    if runtime is not None and runtime.releases == releases:
        return runtime
    _install_runtime_repo(ctx)
    runtime = CandidateRuntime(ctx, releases)
    ctx.candidate_offers = runtime.final_candidate_offers
    ctx._candidate_runtime = runtime
    return runtime


def _install_runtime_repo(ctx) -> None:
    if not getattr(ctx, "_runtime_repo_configured", False):
        configured_repo = getattr(ctx, "_configured_repo", ctx.repo)
        ctx._configured_repo = configured_repo

        def runtime_repo():
            base = configured_repo()
            removed_nodes = NodesRepository(ctx.db).removed_names()
            accounts_repository = AccountsRepository(ctx.db)
            removed_ids = accounts_repository.removed_managed_ids()
            managed = [row for row in accounts_repository.managed_machine_accounts()
                       if row["node"] not in removed_nodes]
            if not managed and not removed_nodes and not removed_ids:
                return base
            managed_ids = {row["id"] for row in managed}
            accounts = {account.id: account for account in base.accounts
                        if account.id not in managed_ids and account.id not in removed_ids
                        and account.node not in removed_nodes}
            for row in managed:
                accounts[row["id"]] = Account(
                    id=row["id"], provider=row["provider"], node=row["node"], tier=row["tier"],
                    roles=list(row["roles"] or []), parallel=int(row["parallel"]),
                    explicit_only=bool(row["explicit_only"]), paused=bool(row["paused"]))
            return replace(base, accounts=list(accounts.values()))

        ctx.repo = runtime_repo
        ctx._runtime_repo_configured = True
    if not getattr(ctx, "_shared_accounts", False):
        base_repo = ctx.repo
        ctx.repo = lambda: shared_accounts.overlay(base_repo(), ctx.db)
        ctx._shared_accounts = True
