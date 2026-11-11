"""Polls share the provider routing answers: one poll refreshes an expired answer, the others do not repeat it
and do not wait for it (the preference is advisory; a claim is checked again without it). JAIF-438."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from aiflow.v4.control import eligibility
from aiflow.v4.control.repositories.accounts import FakeAccountRepository


def service() -> eligibility.ProviderRoutingEligibilityService:
    ctx = SimpleNamespace(db=None, account_repository=FakeAccountRepository())
    return eligibility.ProviderRoutingEligibilityService(ctx, None, lambda *a, **k: [], lambda account: False,
                                                         lambda account, roles: [])


def run_threads(count: int, target) -> list:
    results: list = [None] * count
    start = threading.Barrier(count)

    def worker(index: int) -> None:
        start.wait()
        results[index] = target()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results


def test_concurrent_first_polls_probe_a_peer_once():
    svc, calls = service(), []

    def probe(account, node, **kwargs):
        calls.append((account, node))
        time.sleep(0.2)
        return [{"key": "T-1", "step": "dev"}]

    svc.peer_candidates = probe
    results = run_threads(8, lambda: svc._shared_peer_offers("codex-1", "mac"))
    assert len(calls) == 1
    assert all(result == [{"key": "T-1", "step": "dev"}] for result in results)


def test_an_expired_peer_answer_is_served_while_one_poll_refreshes_it():
    svc, release, started = service(), threading.Event(), threading.Event()
    svc._peer_offers[("codex-1", "mac")] = (time.monotonic() - eligibility.PEER_OFFERS_TTL_S - 1, ["old"])

    def probe(account, node, **kwargs):
        started.set()
        release.wait(timeout=10)
        return ["new"]

    svc.peer_candidates = probe
    refresher = []
    thread = threading.Thread(target=lambda: refresher.append(svc._shared_peer_offers("codex-1", "mac")))
    thread.start()
    assert started.wait(timeout=10)
    assert svc._shared_peer_offers("codex-1", "mac") == ["old"]  # no waiting for the refresh
    release.set()
    thread.join(timeout=10)
    assert refresher == [["new"]] and svc._shared_peer_offers("codex-1", "mac") == ["new"]


def test_takeable_offers_of_a_snapshot_use_the_previous_answer_while_another_poll_computes():
    svc = service()
    snapshot = {"takeable": {}, "lock": threading.RLock(), "accounts": [], "ready": set(), "online": set(),
                "capacity": {}, "compatible": {}}
    svc._last_takeable["claude"] = {("T-1", "dev")}
    held, release = threading.Event(), threading.Event()

    def compute_elsewhere():
        with snapshot["lock"]:
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=compute_elsewhere)
    thread.start()
    assert held.wait(timeout=10)
    assert svc._takeable(snapshot, "claude") == {("T-1", "dev")}
    release.set()
    thread.join(timeout=10)
    assert svc._takeable(snapshot, "claude") == set()  # computed for this snapshot: no ready peer offers anything
    assert svc._last_takeable["claude"] == set()


def test_reset_cache_forgets_the_previous_answers():
    svc = service()
    svc._last_takeable["claude"] = {("T-1", "dev")}
    svc._peer_offers[("codex-1", "mac")] = (time.monotonic(), ["x"])
    svc.reset_cache()
    assert svc._last_takeable == {} and svc._peer_offers == {} and svc._snapshot_cache is None


def test_real_eligibility_service_uses_context_repository_and_paused_overlay(ctx):
    configured_accounts = [
        SimpleNamespace(id="jaif301-paused", provider="claude", paused=True, machines=[]),
        SimpleNamespace(id="jaif301-active", provider="codex", paused=False, machines=[]),
    ]
    ctx.repo = lambda: SimpleNamespace(accounts=configured_accounts)

    class RecordingAccountRepository(FakeAccountRepository):
        def __init__(self):
            super().__init__([
                {"id": "jaif301-paused", "provider": "claude", "status": "idle",
                 "quota": {"windows": {"five_hour": {"utilization": 0.97}}}, "quota_at_risk": False},
                {"id": "jaif301-active", "provider": "codex", "status": "working",
                 "quota": {"windows": {"five_hour": {"utilization": 0.3}}}, "quota_at_risk": True},
                {"id": "jaif301-unconfigured", "provider": "grok", "status": "probing",
                 "quota": {"windows": {"five_hour": {"utilization": 0.5}}},
                 "quota_at_risk": False},
            ])
            self.providers: list[list[str]] = []
            self.ready_reads = 0

        def quota_routing_rows(self, providers):
            self.providers.append(list(providers))
            return super().quota_routing_rows(providers)

        def ready_ids(self):
            self.ready_reads += 1
            return super().ready_ids()

    repository = RecordingAccountRepository()
    ctx.account_repository = repository
    service = eligibility.ProviderRoutingEligibilityService(ctx, None, lambda *a, **k: [], lambda account: False,
                                                              lambda account, roles: [])

    state = service.state(persist=False)
    snapshot = service._snapshot()

    assert service.account_repository is repository
    assert set(state["providers"]) == {"claude", "codex"}
    assert state["providers"]["codex"]["windows"] == [
        {"name": "five_hour", "account": "jaif301-active", "used_pct": 30.0,
         "reset_at": None, "reset": False},
    ]
    assert state["providers"]["codex"]["at_risk_share"] == 1.0
    assert state["providers"]["codex"]["score"] == 55.0
    assert repository.providers == [["claude", "codex"], ["claude", "codex"]]
    assert repository.ready_reads == 1
    assert snapshot["ready"] == {"jaif301-active"}
    assert snapshot["state"]["providers"]["codex"] == state["providers"]["codex"]


def test_failed_repository_snapshot_read_releases_lock_and_retry_succeeds(ctx):
    ctx.repo = lambda: SimpleNamespace(accounts=[])

    class FailsOnceRepository(FakeAccountRepository):
        def __init__(self):
            super().__init__()
            self.failure = RuntimeError("account repository unavailable")
            self.ready_reads = 0

        def ready_ids(self):
            self.ready_reads += 1
            if self.ready_reads == 1:
                raise self.failure
            return super().ready_ids()

    repository = FailsOnceRepository()
    ctx.account_repository = repository
    service = eligibility.ProviderRoutingEligibilityService(ctx, None, lambda *a, **k: [], lambda account: False,
                                                              lambda account, roles: [])

    try:
        service._snapshot()
    except RuntimeError as error:
        assert error is repository.failure
    else:
        raise AssertionError("the repository failure should propagate")

    assert service._snapshot_cache is None
    assert service._snapshot_build.acquire(blocking=False)
    service._snapshot_build.release()
    assert service._snapshot()["ready"] == set()
    assert repository.ready_reads == 2
