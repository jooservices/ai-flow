"""Polls share the provider routing answers: one poll refreshes an expired answer, the others do not repeat it
and do not wait for it (the preference is advisory; a claim is checked again without it). JAIF-438."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from aiflow.v4.control import eligibility


def service() -> eligibility.ProviderRoutingEligibilityService:
    ctx = SimpleNamespace(db=None)
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
