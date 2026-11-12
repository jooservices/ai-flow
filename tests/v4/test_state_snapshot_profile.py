"""Unit tests for the bounded JAIF-436 state snapshot profiling helper."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import state_snapshot_profile as profile


class FakeDatabase:
    def __init__(self) -> None:
        self.fail: Exception | None = None

    def all(self, _sql: str, _params=()):
        if self.fail:
            raise self.fail
        return [{"opaque": "never recorded"}, {"opaque": "never recorded"}]

    def one(self, _sql: str, _params=()):
        if self.fail:
            raise self.fail
        return {"opaque": "never recorded"}

    def run(self, _sql: str, _params=()):
        if self.fail:
            raise self.fail
        return 1


class FakeSnapshots:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cache = {}


def fake_modules(db: FakeDatabase):
    state = SimpleNamespace()
    view = SimpleNamespace()

    def snapshot(_ctx, *, include_signin=False):
        db.all("SELECT opaque")
        return {"include_signin": include_signin, "tickets": [1], "projects": [1]}

    def cached_snapshot(ctx, snapshots, include_signin):
        with snapshots.lock:
            if include_signin in snapshots.cache:
                return snapshots.cache[include_signin]
            result = state.snapshot(ctx, include_signin=include_signin)
            snapshots.cache[include_signin] = result
            return result

    state.snapshot = snapshot
    view.cached_snapshot = cached_snapshot
    return view, state


def make_request(category="burst"):
    return profile.RequestSample(category, True, time.perf_counter(), 1)


def measured_call(profiler, view, snapshots, visible):
    sample = make_request()
    token = profile.ACTIVE_REQUEST.set(sample)
    try:
        result = view.cached_snapshot(None, snapshots, visible)
    finally:
        sample.request_wall_ms = 0.1
        profile.ACTIVE_REQUEST.reset(token)
        profiler._finish(sample)
    return sample, result


def test_visibility_classes_have_separate_build_and_hit_counts_and_restore_patches():
    db = FakeDatabase()
    view, state = fake_modules(db)
    original_cache, original_snapshot, original_all = view.cached_snapshot, state.snapshot, db.all
    snapshots = FakeSnapshots()
    original_lock = snapshots.lock
    profiler = profile.StateProfiler()

    with profiler.instrument(db, view, state):
        with profiler.worker_activity():
            false_build, false_result = measured_call(profiler, view, snapshots, False)
            false_hit, false_shared = measured_call(profiler, view, snapshots, False)
            true_build, true_result = measured_call(profiler, view, snapshots, True)
            true_hit, true_shared = measured_call(profiler, view, snapshots, True)
        assert false_result["include_signin"] is False
        assert false_shared is false_result
        assert true_result["include_signin"] is True
        assert true_shared is true_result
        assert false_build.did_build and not false_hit.did_build
        assert true_build.did_build and not true_hit.did_build
        assert snapshots.lock is not original_lock

    assert view.cached_snapshot is original_cache
    assert state.snapshot is original_snapshot
    assert db.all.__func__ is original_all.__func__
    assert snapshots.lock is original_lock
    visibility = profiler.categories["burst"]["visibility"]
    assert visibility["exclude_signin"] == {
        "requests": 2, "builds": 1, "coalesced_hits": 1, "sql_queries": 1, "returned_rows": 2,
    }
    assert visibility["include_signin"]["builds"] == 1
    assert visibility["include_signin"]["coalesced_hits"] == 1
    assert visibility["include_signin"]["returned_rows"] == 2
    profiled_build = false_build
    assert profiled_build.build_cpu_ms is not None
    assert sum(item["self_thread_cpu_ms"] for item in profiled_build.contributors) <= profiled_build.build_cpu_ms * 1.5


def test_snapshot_and_database_exceptions_propagate_and_restore_then_retry():
    db = FakeDatabase()
    view, state = fake_modules(db)
    original_cache, original_snapshot = view.cached_snapshot, state.snapshot
    snapshots = FakeSnapshots()
    original_lock = snapshots.lock
    profiler = profile.StateProfiler()
    failure = RuntimeError("opaque failure")
    db.fail = failure
    with pytest.raises(RuntimeError) as caught:
        with profiler.instrument(db, view, state):
            measured_call(profiler, view, snapshots, False)
    assert caught.value is failure
    assert view.cached_snapshot is original_cache
    assert state.snapshot is original_snapshot
    assert snapshots.lock is original_lock
    assert db.fail is failure
    db.fail = None
    assert view.cached_snapshot(None, snapshots, False)["include_signin"] is False


def test_request_middleware_resets_context_and_preserves_original_exception():
    async def scenario():
        profiler = profile.StateProfiler()
        capture = profiler.middleware(forced_category="burst")
        failure = LookupError("opaque failure")

        async def call_next(_request):
            raise failure

        with pytest.raises(LookupError) as caught:
            await capture(SimpleNamespace(url=SimpleNamespace(path="/v1/state")), call_next)
        assert caught.value is failure
        assert profile.ACTIVE_REQUEST.get() is None
        assert profiler.categories["burst"]["requests"] == 1
        assert profiler.categories["burst"]["samples"][0]["error_type"] == "LookupError"

    asyncio.run(scenario())


def test_measured_lock_timeout_branch_and_empty_or_failed_proof_are_explicit():
    lock = threading.Lock()
    lock.acquire()
    request = make_request()
    token = profile.ACTIVE_REQUEST.set(request)
    try:
        assert not profile.MeasuredLock(lock).acquire(timeout=0.001)
    finally:
        profile.ACTIVE_REQUEST.reset(token)
        lock.release()
    assert request.lock_timed_out
    assert request.lock_wait_ms is not None
    assert profile._series([]) == {"samples": 0, "min_ms": None, "median_ms": None, "max_ms": None}
    failed = profile._proof_summary({"passed": False, "budgets": [{"key": "x", "passed": False}],
                                     "checks": [{"name": "overlap", "ok": False}]})
    assert failed["status"] == "failed"
    assert failed["failed_budgets"] == ["x"]
    assert failed["failed_checks"] == ["overlap"]
    assert failed["measurements"] == {
        name: {"samples": 0, "status": "not measured"}
        for name in ("console_page_load", "console_page_queries", "console_page_bytes", "project_picker",
                     "ticket_pickup")
    } | {"state_request_wall_ms_uninstrumented": {
        "views_with_samples": 0, "per_view_p95_ms": [], "max_per_view_p95_ms": None,
        "note": "Each view contributes three state requests; its p95 is the maximum of those three.",
    }}


def test_state_request_is_burst_only_when_a_worker_request_overlaps_it():
    async def scenario():
        profiler = profile.StateProfiler()
        capture = profiler.middleware()

        async def call_next(_request):
            with profiler.worker_activity():
                await asyncio.sleep(0)
            return SimpleNamespace(headers={})

        await capture(SimpleNamespace(url=SimpleNamespace(path="/v1/state")), call_next)
        sample = profiler.categories["burst"]["samples"][0]
        assert sample["state_overlapped_worker_request"] is True
        assert profiler.categories["burst"]["state_worker_overlaps"] == 1

        async def no_worker(_request):
            return SimpleNamespace(headers={})

        await capture(SimpleNamespace(url=SimpleNamespace(path="/v1/state")), no_worker)
        assert profiler.categories["other"]["requests"] == 1
        idle_capture = profiler.idle_middleware()
        for _ in range(3):
            await idle_capture(SimpleNamespace(url=SimpleNamespace(path="/v1/state")), no_worker)
        assert len(profiler.categories["idle"]["samples"]) == 3

    asyncio.run(scenario())


def test_harness_wrapper_tracks_worker_http_request_and_restores_middleware():
    async def scenario():
        db = FakeDatabase()
        view, state = fake_modules(db)
        async def original_middleware(_request, call_next):
            return await call_next(_request)

        harness = SimpleNamespace(_count_request_queries=original_middleware,
                                  _claim_session=lambda: None)
        original_claim = harness._claim_session
        profiler = profile.StateProfiler()

        async def response(_request):
            return SimpleNamespace(headers={})

        async def application(_request):
            worker_request = SimpleNamespace(url=SimpleNamespace(path="/v1/work/claim"))
            await harness._count_request_queries(worker_request, response)
            return SimpleNamespace(headers={})

        with profiler.instrument(db, view, state, harness):
            state_request = SimpleNamespace(url=SimpleNamespace(path="/v1/state"))
            await harness._count_request_queries(state_request, application)

        assert profiler.categories["burst"]["state_worker_overlaps"] == 1
        assert harness._count_request_queries is original_middleware
        assert harness._claim_session is original_claim

    asyncio.run(scenario())


def test_function_aggregates_keep_all_bounded_entries_and_separate_categories():
    profiler = profile.StateProfiler()
    for category, offset in (("burst", 0), ("other", 10)):
        sample = make_request(category)
        sample.profiled_function_category = category
        sample.contributors = [
            {"module": "module.py", "line": index, "function": f"function_{index}",
             "self_thread_cpu_ms": float(index + offset)}
            for index in range(10)
        ]
        profiler._finish(sample)

    summary = profiler.json_summary()["largest_snapshot_contributors_thread_cpu_ms_by_category"]
    assert len(profiler.functions["burst"]) == 10
    assert len(profiler.functions["other"]) == 10
    assert summary["burst"][0]["function"] == "function_9"
    assert summary["other"][0]["self_thread_cpu_ms"] > summary["burst"][0]["self_thread_cpu_ms"]


def test_driver_propagates_failure_restores_patches_and_allows_retry(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from aiflow.v4.control import scale_harness
    from aiflow.v4.control import state as state_module
    from aiflow.v4.control.features.business import state_view

    class DriverDatabase(FakeDatabase):
        def one(self, _sql, _params=()):
            return {"n": 0}

    db = DriverDatabase()
    leases = SimpleNamespace(r=SimpleNamespace(ping=lambda: True))

    @contextmanager
    def scratch(_database_url, _redis_url):
        yield db, leases

    original_cache = state_view.cached_snapshot
    original_snapshot = state_module.snapshot
    original_all, original_one, original_run = db.all, db.one, db.run
    monkeypatch.setattr(scale_harness, "scratch", scratch)
    failure = RuntimeError("scratch driver failure")
    calls = 0

    def fail_then_succeed(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return {"passed": False, "budgets": [], "checks": []}, SimpleNamespace()

    monkeypatch.setattr(scale_harness, "run", fail_then_succeed)
    with pytest.raises(RuntimeError) as caught:
        profile._run_proof("unused", "unused", "failure-test", True)
    assert caught.value is failure
    assert state_view.cached_snapshot is original_cache
    assert state_module.snapshot is original_snapshot
    assert db.all.__func__ is original_all.__func__
    assert db.one.__func__ is original_one.__func__
    assert db.run.__func__ is original_run.__func__

    result, _ = profile._run_proof("unused", "unused", "retry-test", False)
    assert calls == 2
    assert result["status"] == "failed"
