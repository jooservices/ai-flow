"""Bounded, opt-in profiler for reproducing the JAIF-436 state snapshot report."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


MAX_SAMPLES = 20
MAX_FUNCTION_PROFILES = 3
ACTIVE_REQUEST: contextvars.ContextVar["RequestSample | None"] = contextvars.ContextVar(
    "jaif436_state_profile_request", default=None)


@dataclass
class RequestSample:
    category: str
    sample: bool
    started: float
    workers_at_start: int
    worker_overlap: bool = False
    build_worker_overlap: bool = False
    build_started: bool = False
    include_signin: bool | None = None
    request_wall_ms: float | None = None
    cached_wall_ms: float | None = None
    build_wall_ms: float | None = None
    build_cpu_ms: float | None = None
    lock_wait_ms: float | None = None
    lock_timed_out: bool = False
    sql_queries: int = 0
    returned_rows: int = 0
    harness_queries: int | None = None
    did_build: bool = False
    profiled_function_category: str | None = None
    error_type: str = ""
    profiler_error_type: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    contributors: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_query(self, rows: int = 0) -> None:
        with self._lock:
            self.sql_queries += 1
            self.returned_rows += rows


class MeasuredLock:
    """Lock-compatible proxy that records acquire wait on the current state request."""

    def __init__(self, lock: Any) -> None:
        self.lock = lock

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        started = time.perf_counter()
        acquired = self.lock.acquire(blocking, timeout)
        request = ACTIVE_REQUEST.get()
        if request is not None:
            request.lock_wait_ms = (time.perf_counter() - started) * 1000
            request.lock_timed_out = not acquired
        return acquired

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> "MeasuredLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class ThreadCpuTracker:
    """Aggregate bounded per-function self CPU from the current thread's profile events."""

    def __init__(self, function_limit: int = 1000) -> None:
        self.function_limit = function_limit
        self.previous = None
        self.last_ns = 0
        self.stack: list[tuple[tuple[str, int, str] | None, int]] = []
        self.elapsed_ns: Counter[tuple[str, int, str]] = Counter()

    def _event(self, frame: Any, event: str, _arg: Any) -> None:
        now_ns = time.thread_time_ns()
        if self.stack and self.stack[-1][0] is not None:
            key, current_ns = self.stack[-1]
            self.stack[-1] = (key, current_ns + max(0, now_ns - self.last_ns))
        self.last_ns = now_ns
        if event == "call":
            code = frame.f_code
            filename = Path(code.co_filename).name
            key = None if filename == "profile_state_snapshot.py" else (
                filename, int(code.co_firstlineno), str(code.co_name))
            self.stack.append((key, 0))
        elif event == "return" and self.stack:
            key, elapsed = self.stack.pop()
            if key is not None and (key in self.elapsed_ns or len(self.elapsed_ns) < self.function_limit):
                self.elapsed_ns[key] += elapsed

    def start(self) -> None:
        self.previous = sys.getprofile()
        self.last_ns = time.thread_time_ns()
        sys.setprofile(self._event)

    def stop(self) -> list[dict[str, Any]]:
        sys.setprofile(self.previous)
        ranked = self.elapsed_ns.most_common(self.function_limit)
        return [{"module": module, "line": line, "function": name,
                 "self_thread_cpu_ms": round(elapsed_ns / 1_000_000, 3)}
                for (module, line, name), elapsed_ns in ranked]


class StateProfiler:
    """Patch existing call boundaries for one run and retain only numeric aggregates."""

    def __init__(self, sample_limit: int = MAX_SAMPLES, function_limit: int = MAX_FUNCTION_PROFILES) -> None:
        self.sample_limit = sample_limit
        self.function_limit = function_limit
        self._guard = threading.Lock()
        self._worker_guard = threading.Lock()
        self._active_workers = 0
        self._request_samples: dict[int, RequestSample] = {}
        self._reserved: dict[str, int] = {}
        self._function_profiles: dict[str, int] = {}
        self._originals: list[tuple[Any, str, Any]] = []
        self._locks: dict[int, tuple[Any, Any]] = {}
        self._installed = False
        self.categories: dict[str, dict[str, Any]] = {}
        self.events: dict[str, int] = {"state_requests": 0, "unclassified_state_requests": 0}
        self.functions: dict[str, dict[tuple[str, int, str], float]] = {}
        self._middleware_original: Any = None
        self._claim_original: Any = None

    def _reserve_function_profile(self, category: str) -> bool:
        with self._guard:
            count = self._function_profiles.get(category, 0)
            if count >= self.function_limit:
                return False
            self._function_profiles[category] = count + 1
            return True

    @contextlib.contextmanager
    def worker_activity(self) -> Iterator[None]:
        with self._worker_guard:
            self._active_workers += 1
            with self._guard:
                for sample in self._request_samples.values():
                    sample.worker_overlap = True
                    if sample.build_started:
                        sample.build_worker_overlap = True
        try:
            yield
        finally:
            with self._worker_guard:
                self._active_workers -= 1

    def _worker_count(self) -> int:
        with self._worker_guard:
            return self._active_workers

    def _start_request(self, sample: RequestSample) -> None:
        with self._worker_guard:
            sample.workers_at_start = self._active_workers
            sample.worker_overlap = self._active_workers > 0
            with self._guard:
                if sum(1 for _ in self._request_samples) < self.sample_limit * 3:
                    self._request_samples[id(sample)] = sample

    def _start_build(self, sample: RequestSample) -> None:
        with self._worker_guard:
            sample.build_started = True
            sample.build_worker_overlap = self._active_workers > 0

    def _end_build(self, sample: RequestSample) -> None:
        with self._worker_guard:
            sample.build_started = False

    def middleware(self, forced_category: str | None = None):
        async def capture(request: Any, call_next: Any) -> Any:
            if request.url.path != "/v1/state":
                return await call_next(request)
            sample = RequestSample(forced_category or "request", False, time.perf_counter(), 0)
            self._start_request(sample)
            sample.sample = True
            with self._guard:
                self.events["state_requests"] += 1
            token = ACTIVE_REQUEST.set(sample)
            response = None
            try:
                response = await call_next(request)
                try:
                    sample.harness_queries = int(response.headers.get("x-aiflow-scale-query-count", "0"))
                except (AttributeError, TypeError, ValueError):
                    sample.harness_queries = None
                return response
            except BaseException as exc:
                sample.error_type = type(exc).__name__
                raise
            finally:
                sample.request_wall_ms = (time.perf_counter() - sample.started) * 1000
                ACTIVE_REQUEST.reset(token)
                self._finish(sample)
        return capture

    def _finish(self, sample: RequestSample) -> None:
        with self._guard:
            self._request_samples.pop(id(sample), None)
            if sample.category == "request":
                sample.category = "burst" if sample.worker_overlap else "other"
            summary = self.categories.setdefault(sample.category, {
                "requests": 0, "builds": 0, "coalesced_hits": 0, "sql_queries": 0,
                "returned_rows": 0, "state_worker_overlaps": 0, "build_worker_overlaps": 0,
                "visibility": {}, "samples": [],
            })
            summary["requests"] += 1
            summary["builds"] += int(sample.did_build)
            summary["coalesced_hits"] += int(not sample.did_build and not sample.error_type)
            summary["sql_queries"] += sample.sql_queries
            summary["returned_rows"] += sample.returned_rows
            summary["state_worker_overlaps"] += int(sample.worker_overlap)
            summary["build_worker_overlaps"] += int(sample.build_worker_overlap)
            if sample.category == "other":
                self.events["unclassified_state_requests"] += 1
            visibility = ("include_signin" if sample.include_signin else "exclude_signin"
                          if sample.include_signin is not None else "unknown")
            visible = summary["visibility"].setdefault(visibility, {
                "requests": 0, "builds": 0, "coalesced_hits": 0, "sql_queries": 0, "returned_rows": 0,
            })
            visible["requests"] += 1
            visible["builds"] += int(sample.did_build)
            visible["coalesced_hits"] += int(not sample.did_build and not sample.error_type)
            visible["sql_queries"] += sample.sql_queries
            visible["returned_rows"] += sample.returned_rows
            if sample.contributors and sample.profiled_function_category:
                self._collect_functions(sample.profiled_function_category, sample.contributors)
            if sample.sample and len(summary["samples"]) < self.sample_limit:
                summary["samples"].append(self._safe_sample(sample))

    @staticmethod
    def _safe_sample(sample: RequestSample) -> dict[str, Any]:
        return {
            "request_wall_ms": _rounded(sample.request_wall_ms),
            "cached_snapshot_wall_ms": _rounded(sample.cached_wall_ms),
            "snapshot_build_wall_ms": _rounded(sample.build_wall_ms),
            "snapshot_build_thread_cpu_ms": _rounded(sample.build_cpu_ms),
            "snapshot_lock_wait_ms": _rounded(sample.lock_wait_ms),
            "state_overlapped_worker_request": sample.worker_overlap,
            "snapshot_build_overlapped_worker_request": sample.build_worker_overlap,
            "lock_timed_out": sample.lock_timed_out,
            "include_signin": sample.include_signin,
            "cache_result": "build" if sample.did_build else "coalesced_hit",
            "sql_queries": sample.sql_queries,
            "returned_rows": sample.returned_rows,
            "harness_sql_queries": sample.harness_queries,
            "snapshot_counts": sample.counts,
            "error_type": sample.error_type or None,
            "profiler_error_type": sample.profiler_error_type or None,
            "contributors_thread_cpu_ms": sample.contributors,
        }

    def _ensure_lock(self, snapshots: Any) -> None:
        with self._guard:
            identity = id(snapshots)
            if identity not in self._locks:
                original = snapshots.lock
                proxy = MeasuredLock(original)
                self._locks[identity] = (snapshots, original)
                snapshots.lock = proxy

    def _snapshot_wrapper(self, original: Any):
        def measured(ctx: Any, *args: Any, **kwargs: Any) -> Any:
            sample = ACTIVE_REQUEST.get()
            if sample is None:
                return original(ctx, *args, **kwargs)
            sample.did_build = True
            self._start_build(sample)
            started_wall = time.perf_counter()
            started_cpu = time.thread_time()
            tracker = None
            if sample.build_worker_overlap and self._reserve_function_profile("burst"):
                sample.profiled_function_category = "burst"
                tracker = ThreadCpuTracker()
                tracker.start()
            try:
                result = original(ctx, *args, **kwargs)
                sample.counts = _snapshot_counts(result)
                return result
            except BaseException as exc:
                sample.error_type = type(exc).__name__
                raise
            finally:
                self._end_build(sample)
                if tracker is not None:
                    try:
                        sample.contributors = tracker.stop()
                    except Exception as exc:  # profiling failure must not replace the application result
                        sample.profiler_error_type = type(exc).__name__
                sample.build_wall_ms = (time.perf_counter() - started_wall) * 1000
                sample.build_cpu_ms = (time.thread_time() - started_cpu) * 1000
        return measured

    def _collect_functions(self, category: str, contributors: list[dict[str, Any]]) -> None:
        aggregate = self.functions.setdefault(category, {})
        for item in contributors:
            key = (item["module"], item["line"], item["function"])
            if key in aggregate or len(aggregate) < 1000:
                aggregate[key] = aggregate.get(key, 0.0) + item["self_thread_cpu_ms"]

    def _cache_wrapper(self, original: Any):
        def measured(ctx: Any, snapshots: Any, include_signin: bool) -> Any:
            sample = ACTIVE_REQUEST.get()
            self._ensure_lock(snapshots)
            if sample is not None:
                sample.include_signin = bool(include_signin)
            started = time.perf_counter()
            try:
                return original(ctx, snapshots, include_signin)
            except BaseException as exc:
                if sample is not None:
                    sample.error_type = type(exc).__name__
                raise
            finally:
                if sample is not None:
                    sample.cached_wall_ms = (time.perf_counter() - started) * 1000
        return measured

    @staticmethod
    def _db_wrapper(original: Any, mode: str):
        def measured(*args: Any, **kwargs: Any) -> Any:
            sample = ACTIVE_REQUEST.get()
            if sample is not None:
                sample.add_query()
            result = original(*args, **kwargs)
            if sample is not None:
                rows = len(result) if mode == "all" else int(result is not None) if mode == "one" else 0
                if rows:
                    with sample._lock:
                        sample.returned_rows += rows
            return result
        return measured

    def install(self, db: Any, state_view: Any, state_module: Any, harness: Any | None = None) -> None:
        if self._installed:
            raise RuntimeError("state profiler is already installed")
        try:
            for name, mode in (("all", "all"), ("one", "one"), ("run", "run")):
                original = getattr(db, name)
                self._originals.append((db, name, original))
                setattr(db, name, self._db_wrapper(original, mode))
            original_snapshot = state_module.snapshot
            self._originals.append((state_module, "snapshot", original_snapshot))
            state_module.snapshot = self._snapshot_wrapper(original_snapshot)
            original_cache = state_view.cached_snapshot
            self._originals.append((state_view, "cached_snapshot", original_cache))
            state_view.cached_snapshot = self._cache_wrapper(original_cache)
            if harness is not None:
                self._install_harness(harness)
            self._installed = True
        except BaseException:
            self.restore()
            raise

    def _install_harness(self, harness: Any) -> None:
        original_middleware = harness._count_request_queries
        self._middleware_original = original_middleware

        async def measured_middleware(request: Any, call_next: Any) -> Any:
            path = request.url.path
            worker_request = (path in {"/v1/work/candidates", "/v1/work/claim"}
                              or (path.startswith("/v1/runs/") and path.endswith("/heartbeat")))
            if worker_request:
                with self.worker_activity():
                    return await original_middleware(request, call_next)
            return await self.middleware()(request, lambda _request: original_middleware(request, call_next))

        self._originals.append((harness, "_count_request_queries", original_middleware))
        harness._count_request_queries = measured_middleware

    def restore(self) -> None:
        for snapshots, original_lock in self._locks.values():
            snapshots.lock = original_lock
        self._locks.clear()
        for target, name, original in reversed(self._originals):
            setattr(target, name, original)
        self._originals.clear()
        self._middleware_original = None
        self._claim_original = None
        self._installed = False

    @contextlib.contextmanager
    def instrument(self, db: Any, state_view: Any, state_module: Any,
                   harness: Any | None = None) -> Iterator["StateProfiler"]:
        self.install(db, state_view, state_module, harness)
        try:
            yield self
        finally:
            self.restore()

    def idle_middleware(self):
        return self.middleware(forced_category="idle")

    def json_summary(self) -> dict[str, Any]:
        contributors = {}
        for category, functions in self.functions.items():
            contributors[category] = [
                {"module": module, "line": line, "function": function,
                 "self_thread_cpu_ms": round(cpu_ms, 3)}
                for (module, line, function), cpu_ms in sorted(
                    functions.items(), key=lambda item: item[1], reverse=True)[:12]
            ]
        return {
            "events": dict(self.events),
            "categories": self.categories,
            "function_profiles_by_category": dict(self._function_profiles),
            "largest_snapshot_contributors_thread_cpu_ms_by_category": contributors,
            "sample_limit_per_category": self.sample_limit,
            "function_profile_limit_per_category": self.function_limit,
        }


def _snapshot_counts(data: Any) -> dict[str, int]:
    if not isinstance(data, dict):
        return {}
    return {key: len(value) for key, value in data.items()
            if key in {"projects", "nodes", "accounts", "tickets", "active_runs", "recent_runs", "events"}
            and isinstance(value, list)}


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _series(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "min_ms": None, "median_ms": None, "max_ms": None}
    return {"samples": len(values), "min_ms": round(min(values), 3),
            "median_ms": round(statistics.median(values), 3), "max_ms": round(max(values), 3)}


def _proof_summary(report: dict[str, Any]) -> dict[str, Any]:
    budgets = report.get("budgets") or []
    checks = report.get("checks") or []
    return {
        "status": "passed" if report.get("passed") is True else "failed",
        "passed": report.get("passed") is True,
        "failed_budgets": [item.get("key", "unknown") for item in budgets if not item.get("passed")],
        "budget_verdicts": [{key: item.get(key) for key in
                              ("key", "measured", "effective_limit", "unit", "passed")}
                             for item in budgets],
        "failed_checks": [item.get("name", "unknown") for item in checks if not item.get("ok")],
        "scenario": report.get("scenario", {}),
        "counts": report.get("counts", {}),
        "observed": report.get("observed", {}),
        "environment": {key: (report.get("environment") or {}).get(key)
                        for key in ("python", "platform", "cpus", "api_execution", "persistence")},
        "duration_s": report.get("duration_s"),
        "measurements": _safe_harness_measurements(report.get("measurements") or {}),
    }


def _safe_harness_measurements(measurements: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for name in ("console_page_load", "console_page_queries", "console_page_bytes", "project_picker", "ticket_pickup"):
        item = measurements.get(name)
        if not isinstance(item, dict):
            safe[name] = {"samples": 0, "status": "not measured"}
            continue
        safe[name] = {key: item.get(key) for key in ("value", "unit", "statistic", "samples", "p50", "max")}
    views = (measurements.get("console_page_load") or {}).get("views") or {}
    state_p95 = [float(view["calls_p95"]["/v1/state"]) * 1000 for view in views.values()
                 if isinstance(view, dict) and "/v1/state" in (view.get("calls_p95") or {})]
    safe["state_request_wall_ms_uninstrumented"] = {
        "views_with_samples": len(state_p95),
        "per_view_p95_ms": [round(value, 3) for value in state_p95],
        "max_per_view_p95_ms": round(max(state_p95), 3) if state_p95 else None,
        "note": "Each view contributes three state requests; its p95 is the maximum of those three.",
    }
    return safe


def _dataset_counts(db: Any) -> dict[str, int]:
    tickets = db.one("SELECT count(*) AS n FROM tickets")
    runs = db.one("SELECT count(*) AS n FROM runs")
    running = db.one("SELECT count(*) AS n FROM runs WHERE status='running'")
    return {"tickets": int((tickets or {}).get("n", 0)), "runs": int((runs or {}).get("n", 0)),
            "running_runs": int((running or {}).get("n", 0))}


def _service_latency(db: Any, leases: Any) -> dict[str, Any]:
    database, redis = [], []
    for _ in range(5):
        started = time.perf_counter()
        db.one("SELECT 1 AS ok")
        database.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        leases.r.ping()
        redis.append((time.perf_counter() - started) * 1000)
    return {"database_select_1_ms": _series(database), "redis_ping_ms": _series(redis)}


@contextlib.contextmanager
def _scale_proof_migrations() -> Iterator[None]:
    """Apply the same scratch-index fixture used by test_scale_proof before the driver creates its schema."""
    from aiflow.v4.control import db as control_db

    migration_parent = REPO_ROOT / ".tmp"
    migration_parent.mkdir(exist_ok=True)
    original_migrations = control_db.MIGRATIONS
    fixture = REPO_ROOT / "tests/v4/fixtures/scale_scratch_index.sql"
    with tempfile.TemporaryDirectory(prefix="jaif436-migrations-", dir=migration_parent) as temporary:
        migrations = Path(temporary)
        for migration in original_migrations.glob("*.sql"):
            (migrations / migration.name).symlink_to(migration)
        memory_migration = original_migrations / "002_memory.sql"
        memory_sql = memory_migration.read_text(encoding="utf-8")
        create_index = "CREATE INDEX IF NOT EXISTS memory_project ON memory"
        if create_index not in memory_sql:
            raise RuntimeError("expected memory index migration clause is missing")
        memory_sql = memory_sql.replace(create_index, "CREATE INDEX memory_project ON memory", 1)
        patched_memory_migration = migrations / memory_migration.name
        patched_memory_migration.unlink()
        patched_memory_migration.write_text(
            memory_sql + "\n" + fixture.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            control_db.MIGRATIONS = migrations
            yield
        finally:
            control_db.MIGRATIONS = original_migrations


def _idle_reads(ctx: Any, profiler: StateProfiler) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session

    app = create_app(ctx)
    app.middleware("http")(profiler.idle_middleware())
    session = issue_session("scale-proof@example.test", "administrator", 2_000_000_000,
                            ctx.settings.console_session_secret)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, session)
        statuses = [client.get("/v1/state").status_code for _ in range(3)]
    return {"samples_requested": 3, "http_statuses": statuses,
            "snapshot_data_counts": _dataset_counts(ctx.db)}


def _run_proof(database_url: str, redis_url: str, label: str, profile: bool) -> tuple[dict[str, Any], Any]:
    from aiflow.v4.control import scale_harness
    from aiflow.v4.control.features.business import state_view
    from aiflow.v4.control import state as state_module

    output: dict[str, Any] = {"label": label, "instrumented": profile, "status": "failed"}
    workdir = Path(".tmp") / f"profile-work-{label}"
    workdir.mkdir(parents=True, exist_ok=True)
    profiler = StateProfiler()
    started = time.perf_counter()
    try:
        with _scale_proof_migrations(), scale_harness.scratch(database_url, redis_url) as (db, leases):
            if profile:
                with profiler.instrument(db, state_view, state_module, scale_harness):
                    report, ctx = scale_harness.run(db, leases, workdir)
                    output["proof"] = _proof_summary(report)
                    idle = _idle_reads(ctx, profiler)
                    output["idle"] = idle
                    output["profile"] = profiler.json_summary()
            else:
                report, ctx = scale_harness.run(db, leases, workdir)
                output["proof"] = _proof_summary(report)
            output["dataset_after_proof"] = _dataset_counts(db)
            if profile:
                output["service_latency"] = _service_latency(db, leases)
            output["status"] = "passed" if report.get("passed") is True else "failed"
    finally:
        output["elapsed_s_including_idle_and_setup"] = round(time.perf_counter() - started, 3)
        profiler.restore()
    return output, profiler


def main() -> int:
    database_url = os.environ.get("AIFLOW4_TEST_DATABASE_URL", "")
    redis_url = os.environ.get("AIFLOW4_TEST_REDIS_URL", "")
    if not database_url or not redis_url:
        result = {"status": "failed", "failure_type": "TestServicesUnavailable"}
        Path(".tmp/state-snapshot-profile.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        return 2
    host = {"python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "cpu_count": os.cpu_count()}
    if "--profile-only" in sys.argv:
        previous = json.loads(Path(".tmp/state-snapshot-profile-initial.json").read_text())
        baseline = previous["runs"]["uninstrumented"]
        profiled, _ = _run_proof(database_url, redis_url, "profiled", True)
        result = previous
        result["created_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result["host"] = host
        result["runs"]["profiled"] = profiled
        result["comparison"] = _proof_comparison(baseline, profiled)
    else:
        baseline, _ = _run_proof(database_url, redis_url, "uninstrumented", False)
        profiled, _ = _run_proof(database_url, redis_url, "profiled", True)
        result = {"ticket": "JAIF-436", "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "host": host, "runs": {"uninstrumented": baseline, "profiled": profiled},
                  "comparison": _proof_comparison(baseline, profiled)}
    path = Path(".tmp/state-snapshot-profile.json")
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if baseline.get("status") == "passed" and profiled.get("status") == "passed" else 1


def _proof_comparison(baseline: dict[str, Any], profiled: dict[str, Any]) -> dict[str, Any]:
    base_measurements = (baseline.get("proof") or {}).get("measurements") or {}
    profile_measurements = (profiled.get("proof") or {}).get("measurements") or {}
    base_state = (base_measurements.get("state_request_wall_ms_uninstrumented") or {}).get("max_per_view_p95_ms")
    prof_state_samples = [sample["request_wall_ms"]
                          for item in ((profiled.get("profile") or {}).get("categories") or {}).values()
                          for sample in item.get("samples", [])
                          if sample.get("request_wall_ms") is not None]
    return {
        "uninstrumented_max_view_state_p95_ms": base_state,
        "profiled_state_request_wall_ms": _series(prof_state_samples),
        "uninstrumented_console_worst_view_p95_s": (base_measurements.get("console_page_load") or {}).get("value"),
        "profiled_console_worst_view_p95_s": (profile_measurements.get("console_page_load") or {}).get("value"),
        "interpretation": "Separate scale-proof runs on equivalent default seeds; host and database cache state can vary.",
    }


if __name__ == "__main__":
    sys.exit(main())
