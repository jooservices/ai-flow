"""The scale-proof scenario: 100 projects, 50 machines and 200 simulated AI sessions driven with ticket traffic.

Everything runs against a real control API (FastAPI app, PostgreSQL, Redis leases), through the same HTTP calls the
node agents and the console make. Machines and AI sessions are simulated: a session claims a ticket, heartbeats,
reports `done` and releases the lease, without any AI provider or real machine.

The traffic is a pure function of the `Scenario` (`plan`), seeded, so two runs with the same configuration drive
exactly the same tickets in the same order (`plan_digest` is equal) and produce the same counts; only the timings
differ, within the noise of the host. One run:

  1. writes a control repo (100 projects, 50 nodes serving 4 projects each, 4 sessions per node), joins and
     heartbeats every node, loads a ticket history per project;
  2. in waves, makes tickets ready (some held, some blocked by an earlier ticket) and lets every session claim one
     per round; at the busiest moment of the first wave it measures all console views and
     the project picker; the pickup time of every ticket is measured from "ready" to "claim granted";
  3. checks correct behaviour at this size (each ticket picked exactly once, held tickets untouched, blocked
     tickets after their blocker, leases exclusive, fleet and projects fully visible);
  4. evaluates the budgets of `scale_proof` and returns the report.

Docs: docs/v4/scale-proof.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from threading import Barrier, Condition, Event, Lock, Thread
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import scale_proof
from . import system_check
from .scale_proof import BUDGETS, Budget, percentile

PROVIDERS = ("codex", "claude", "opencode", "grok")
REQUIRED_JIRA_STATUSES = ("Backlog", "Intake", "To Do", "In Progress", "In Verify", "Needs owner", "Done")
PRIORITIES = ("Highest", "High", "Medium", "Low")
HISTORY_STATUSES = (("Done", 70), ("In Progress", 10), ("In Verify", 10), ("Needs owner", 5))
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Every top-level console tab in console.html, with the additional data calls its initial view requests.
# Common calls mirror the real browser boot sequence; view-specific calls mirror setView/load* handlers.
COMMON_CALLS = (
    "/v1/console/session", "/v1/console/preferences", "/v1/console/layouts", "/v1/state", "/v1/services",
    "/v1/analytics/daily?days=14", "/v1/analytics/conflicts?days=14", "/v1/analytics/usage/daily?days=14",
    "/v1/licensing/status", "/v1/master/autonomy",
)
PICKER = "/v1/projects"
CONSOLE_REQUESTS_PER_VIEW = 6
PAGES: dict[str, tuple[str, ...]] = {
    "/cc": ("/v1/dashboard/waiting", "/v1/dashboard/disagreements", "/v1/dashboard/quota", "/v1/capacity",
            "/v1/escalations"),
    "/work": ("/v1/dashboard/waiting", PICKER),
    "/delivery": ("/v1/dashboard/waiting", "/v1/analytics/daily?days=14", "/v1/analytics/conflicts?days=14", PICKER),
    "/sa": (),
    # The lead view accepts an Epic key, while `all` is only the work-board filter.
    "/lead": ("/v1/projects/SCP001/report",),
    "/business": ("/v1/business/SCP001/change-requests",),
    "/review": ("/v1/reviews",),
    "/stakeholder": ("/v1/stakeholder/SCP001",),
    "/machines": ("/v1/services",),
    "/users": ("/v1/console/users",),
    "/knowledge": ("/v1/knowledge/resources?include_archived=true",
                   "/v1/knowledge/items?project=SCP001&q=&include_retired=true"),
    "/backup": ("/v1/backups",),
    "/integrations": ("/v1/integrations/jira", "/v1/integrations/github/connections"),
    "/projects": ("/v1/projects", "/v1/environment/missing", "/v1/environment/scan"),
    "/licensing": ("/v1/licensing", "/v1/licensing/usage"),
    "/notifications": ("/v1/notifications",),
    "/settings": ("/v1/runtime-settings",),
    "/system-check": ("/v1/system-check",),
    "/security": ("/v1/security/mfa", "/v1/security/settings", "/v1/security/users",
                  "/v1/security/sessions", "/v1/security/audit?limit=100"),
    "/support": (),
}
JOIN_KEY = "scale-proof-join"


@dataclass(frozen=True)
class Scenario:
    projects: int = 100
    machines: int = 50
    sessions: int = 200
    wave_sizes: tuple[int, ...] = (200, 80, 80)  # tickets that become ready together: a morning burst, then trickles
    history_per_project: int = 20
    seed: int = 236
    page_samples: int = 3
    picker_samples: int = 5
    claim_concurrency: int = 20
    measurement_concurrency: int = 2
    measurement_min_active: int = 150
    max_rounds: int = 20


# --- the plan: a pure function of the scenario ------------------------------------------------------------------
def project_keys(sc: Scenario) -> list[str]:
    return [f"SCP{i:03d}" for i in range(1, sc.projects + 1)]


def node_names(sc: Scenario) -> list[str]:
    return [f"scale-node-{i:02d}" for i in range(1, sc.machines + 1)]


def serving(sc: Scenario) -> dict[str, list[str]]:
    """Project -> nodes: every project is served by two machines, every machine serves about four projects."""
    nodes, keys = node_names(sc), project_keys(sc)
    return {key: sorted({nodes[i % sc.machines], nodes[(i + 1) % sc.machines]}) for i, key in enumerate(keys)}


def sessions_of(sc: Scenario) -> list[dict[str, str]]:
    """Model 200 worker sessions on 100 licensed AI accounts (two concurrent sessions per account)."""
    if sc.machines < 1 or sc.sessions < 1:
        return []
    account_count = min(sc.sessions, 100)
    sessions_per_account = max(1, math.ceil(sc.sessions / account_count))
    sessions_per_node = max(1, math.ceil(sc.sessions / sc.machines))
    nodes = node_names(sc)
    out = []
    for index in range(sc.sessions):
        account = index // sessions_per_account
        node = nodes[min(index // sessions_per_node, len(nodes) - 1)]
        provider = PROVIDERS[account % len(PROVIDERS)]
        out.append({"id": f"scale-{provider}-{account + 1:03d}", "provider": provider, "node": node,
                    "session": f"scale-session-{index + 1:03d}"})
    return out


def fleet_poll_order(sessions: list[dict[str, str]]) -> list[dict[str, str]]:
    """Interleave queued worker polls across machines instead of draining the first machine group first."""
    by_node: dict[str, deque[dict[str, str]]] = {}
    for session in sessions:
        by_node.setdefault(session["node"], deque()).append(session)
    nodes = sorted(by_node)
    ordered: list[dict[str, str]] = []
    while any(by_node.values()):
        for node in nodes:
            if by_node[node]:
                ordered.append(by_node[node].popleft())
    return ordered


def plan(sc: Scenario) -> dict[str, Any]:
    rng = random.Random(sc.seed)
    keys = project_keys(sc)
    weights = [1 + (i * 37) % 11 for i in range(len(keys))]  # some projects are far busier than others
    counter = {k: 0 for k in keys}
    history: list[dict[str, Any]] = []
    for k in keys:
        for _ in range(sc.history_per_project):
            counter[k] += 1
            status = rng.choices([s for s, _ in HISTORY_STATUSES], [w for _, w in HISTORY_STATUSES])[0]
            history.append({"key": f"{k}-{counter[k]}", "project": k, "status": status})
    waves = []
    for wave_no, size in enumerate(sc.wave_sizes):
        tickets: list[dict[str, Any]] = []
        for index in range(size):
            # The first burst gives every project two tickets before skewed traffic starts. Every project is served
            # by two machines, so one ticket per project can leave some of the 150 machine slots without enough
            # eligible work to measure the fleet. Later waves keep the weighted distribution and held/blocked mix.
            covered = wave_no == 0 and index < len(keys)
            balanced_burst = wave_no == 0 and index < 2 * len(keys)
            if covered:
                k = keys[index]
            elif balanced_burst:
                k = keys[index - len(keys)]
            else:
                k = rng.choices(keys, weights)[0]
            counter[k] += 1
            t = {"key": f"{k}-{counter[k]}", "project": k, "priority": rng.choices(PRIORITIES, (5, 20, 60, 15))[0],
                 "held": False, "blocked_by": []}
            if not covered:
                roll = rng.random()
                if roll < 0.05:
                    t["held"] = True
                elif roll < 0.10:
                    free = [x for x in tickets if x["project"] == k and not x["held"] and not x["blocked_by"]]
                    if free:
                        t["blocked_by"] = [free[rng.randrange(len(free))]["key"]]
            tickets.append(t)
        waves.append(tickets)
    body = {"scenario": asdict(sc), "history": history, "waves": waves}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {**body, "digest": digest}


# --- fleet ------------------------------------------------------------------------------------------------------
def write_control_repo(root: Path, sc: Scenario) -> None:
    (root / "nodes").mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir(exist_ok=True)
    (root / "policy").mkdir(exist_ok=True)
    (root / "versions.lock").write_text("schema: 1\ntools: {claude_code: 2.1.305}\n")
    (root / "policy" / "default.yaml").write_text(
        "version: 1\ncommands:\n  allow: ['npm *']\n  deny: ['git push*', 'sudo *']\nfiles:\n  write_within: worktree\n")
    (root / "profiles.yaml").write_text(
        "providers:\n  claude: {models: {middle: sonnet}}\n  codex: {models: {middle: gpt-6-luna}}\n")
    sessions = sessions_of(sc)
    accounts_by_id = {session["id"]: session for session in sessions}
    account_parallel = {account_id: sum(session["id"] == account_id for session in sessions)
                        for account_id in accounts_by_id}
    accounts = "accounts:\n" + "".join(
        f"  - {{id: {a['id']}, provider: {a['provider']}, node: {a['node']}, tier: middle, parallel: {account_parallel[a['id']]}, roles: [dev]}}\n"
        for a in accounts_by_id.values())
    (root / "accounts.yaml").write_text(accounts)
    hosts = serving(sc)
    by_node: dict[str, list[str]] = {n: [] for n in node_names(sc)}
    for project, nodes in hosts.items():
        for n in nodes:
            by_node[n].append(project)
    per = max(1, sc.sessions // sc.machines)
    for i, (node, projects) in enumerate(by_node.items()):
        (root / "nodes" / f"{node}.yaml").write_text(
            f"name: {node}\nip: 10.60.{i // 250}.{i % 250 + 1}\nslots: {per}\nprojects: [{', '.join(projects)}]\n")
    for i, key in enumerate(project_keys(sc)):
        (root / "projects" / f"{key}.yaml").write_text(
            f"key: {key}\nrepo: jooservices/scale-{key.lower()}\nbase: develop\nchecks: [npm test]\n"
            f"priority: {1 + i % 5}\n")


class SimJira:
    """The few Jira calls the claim path and the finding tickets make; nothing leaves the process."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.comments: list[tuple[str, str]] = []

    def projects(self) -> list[dict[str, str]]:
        return [{"key": key, "name": key} for key in project_keys(Scenario())]

    def project_statuses(self, _key: str) -> list[str]:
        return list(REQUIRED_JIRA_STATUSES)

    def project_write_permissions(self, _key: str) -> dict[str, bool]:
        return {"BROWSE_PROJECTS": True, "CREATE_ISSUES": True, "EDIT_ISSUES": True, "LINK_ISSUES": True,
                "TRANSITION_ISSUES": True, "ADD_COMMENTS": True}

    def create_issue(self, project, summary, description, issuetype="Story", labels=None, parent="", **_):
        key = f"{project}-{9000 + len(self.created)}"
        self.created.append({"key": key, "project": project, "summary": summary, "description": description,
                             "issuetype": issuetype, "labels": list(labels or [])})
        return key

    def status(self, key: str) -> str:
        return "Created"

    def comment(self, key: str, text: str) -> str:
        self.comments.append((key, text))
        return str(len(self.comments))

    def recent_comments(self, key: str, limit: int = 10) -> list[dict]:
        return []

    def add_worklog(self, key, seconds, started, comment) -> None:
        return None


class SimGitHub:
    """A local GitHub App stand-in that reports the scenario's configured repos and required permissions."""

    kind = "app"
    org = "jooservices"

    def __init__(self, repositories: list[str]) -> None:
        self.meta = {"installation_id": 1, "name": "Scale proof", "account_type": "Organization"}
        self.repositories = [{"full_name": name} for name in repositories]
        self.permissions = {"contents": "write", "pull_requests": "write", "workflows": "write",
                            "actions": "read", "checks": "read", "statuses": "read",
                            "administration": "write"}

    def installation_repositories(self) -> list[dict[str, str]]:
        return list(self.repositories)

    def installation_details(self, installation_id: int) -> dict[str, Any]:
        return {"id": installation_id, "permissions": dict(self.permissions)}

    def branch_exists(self, _repository: str, branch: str) -> bool:
        return branch == "develop"

    def bot_identity(self) -> tuple[str, str]:
        return "AI Flow scale proof", "scale-proof@example.test"


def build_context(db, leases, root: Path, sc: Scenario, jira: Any = None):
    from ..core.controlrepo import ControlRepo
    from ..core.settings import Settings
    from .api import Ctx
    from .events import Events
    from .jira_connect import JiraAuth, SecretStore
    write_control_repo(root, sc)
    snap = ControlRepo.load(root)
    settings = Settings(join_token=JOIN_KEY, join_control_url="https://control.scale.test", reviewer_token="x" * 16,
                        owner_token="owner-scale-proof", lease_ms=90_000, env="scale-proof")
    settings.jira_email = "scale-proof@example.test"
    settings.jira_token = "simulated-jira-token"
    settings.jira_secret_dir = str(root / "jira-secrets")
    ctx = Ctx(settings=settings, db=db, leases=leases, events=Events(db), repo=lambda: snap,
              jira=jira if jira is not None else SimJira())
    ctx.settings.github_org = "jooservices"
    ctx.github = SimGitHub([f"jooservices/scale-{key.lower()}" for key in project_keys(sc)])
    ctx.jira_auth = JiraAuth(settings, SecretStore(settings.jira_secret_dir)).bind(ctx)
    return ctx


def _activate_business_license(ctx) -> None:
    """Give the 100-account scenario a real, short-lived Business entitlement through the normal verifier."""
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from . import licensing

    licensing.initialize(ctx.db)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    ctx.settings.license_public_key = public
    installation = licensing.snapshot(ctx.db, public)["installation_id"]
    token = jwt.encode({"plan": "business", "exp": int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()),
                        "installation_id": installation}, private, algorithm="EdDSA")
    licensing.activate(ctx.db, token, public, "scale-proof-test")


def _load_history(db, sc: Scenario, pl: dict[str, Any], sessions: list[dict[str, str]]) -> None:
    hosts = serving(sc)
    by_node = {s["node"]: s for s in sessions}
    with db.pool.connection() as c:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO tickets (key, project, status, labels, summary, description, priority, jira_created,"
                " jira_updated, synced_at, done_at) VALUES (%s,%s,%s,%s,%s,%s,'Medium',%s,%s,%s,%s)",
                [(t["key"], t["project"], t["status"], [], f"Earlier work {t['key']}", "Acceptance criteria:\n- AC1: ok",
                  EPOCH + timedelta(seconds=i), EPOCH + timedelta(seconds=i), EPOCH + timedelta(seconds=i),
                  EPOCH + timedelta(seconds=i) if t["status"] == "Done" else None)
                 for i, t in enumerate(pl["history"])])
            cur.executemany(
                "INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status, outcome,"
                " started_at, ended_at) VALUES (%s,%s,%s,'dev',%s,%s,%s,1,'finished','done',%s,%s)",
                [(str(uuid.uuid4()), t["key"], t["project"], by_node[hosts[t["project"]][0]]["id"],
                  by_node[hosts[t["project"]][0]]["provider"], hosts[t["project"]][0],
                  EPOCH + timedelta(seconds=i), EPOCH + timedelta(seconds=i + 60))
                 for i, t in enumerate(pl["history"]) if t["status"] == "Done"])


def _insert_wave(db, wave: list[dict[str, Any]], offset: int) -> None:
    with db.pool.connection() as c:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO tickets (key, project, status, labels, summary, description, priority, blocked_by,"
                " jira_created) VALUES (%s,%s,'To Do',%s,%s,%s,%s,%s,%s)",
                [(t["key"], t["project"], ["aiflow-ready", "aiflow-hold"] if t["held"] else ["aiflow-ready"],
                  f"Traffic {t['key']}", "Acceptance criteria:\n- AC1: works", t["priority"], t["blocked_by"],
                  EPOCH + timedelta(days=1, seconds=offset + i)) for i, t in enumerate(wave)])


# --- the run ----------------------------------------------------------------------------------------------------
def _timed(client, path: str, forwarded_ip: str = "") -> tuple[float, int, str]:
    start = time.perf_counter()
    headers = {"X-Forwarded-For": forwarded_ip} if forwarded_ip else None
    r = client.get(path, headers=headers)
    detail = ""
    if r.status_code != 200:
        try:
            body = r.json()
            detail = str(body.get("detail", body.get("error", "")))[:160]
        except (ValueError, AttributeError):
            detail = r.text[:160]
    return time.perf_counter() - start, r.status_code, detail


def _claim_session(client, session: dict[str, str], headers: dict[str, dict[str, str]],
                   gate: Barrier | None = None, worker_request: Callable | None = None,
                   on_claim: Callable | None = None, max_attempts: int = 16) -> dict[str, Any]:
    """One simulated worker polls and claims through the app, retrying a lost candidate race."""
    if gate is not None:
        gate.wait(timeout=30)
    h = headers.get(session["node"])
    if h is None:
        return {"session": session, "conflicts": 0, "error": f"node {session['node']} did not join"}
    call = worker_request or (lambda method, path, **kwargs: getattr(client, method)(path, **kwargs))
    conflicts = 0
    for attempt in range(max(1, max_attempts)):
        started = time.perf_counter()
        candidates = call("get", "/v1/work/candidates",
                          params={"account": session["id"], "worker_id": session["session"]}, headers=h)
        if candidates.status_code != 200:
            return {"session": session, "conflicts": conflicts,
                    "error": f"/v1/work/candidates answered HTTP {candidates.status_code}"}
        tickets = candidates.json().get("tickets", [])
        if not tickets:
            return {"session": session, "conflicts": conflicts}
        # Match the real worker loop: try each offered ticket before polling candidates again. When several
        # workers receive the same list, a lease conflict on one ticket should not force another full eligibility
        # scan while other tickets from that list are still available.
        for candidate in tickets:
            response = call("post", "/v1/work/claim", json={"ticket": candidate["key"],
                                                              "account": session["id"],
                                                              "step": candidate["step"]}, headers=h)
            claimed_at = time.perf_counter()
            if response.status_code == 200:
                claim = response.json()
                if on_claim:
                    on_claim(session, claim)
                return {"session": session, "claim": claim, "claimed_at": claimed_at,
                        "interval": (started, claimed_at), "conflicts": conflicts}
            if response.status_code != 409:
                return {"session": session, "conflicts": conflicts,
                        "error": f"/v1/work/claim answered HTTP {response.status_code}"}
            conflicts += 1
        if attempt + 1 < max_attempts:
            # If every offered ticket lost a race, add a small stable jitter before requesting a fresh list.
            seed = sum(f"{session['id']}:{session.get('session', '')}".encode("utf-8"))
            time.sleep((2 + (seed + attempt * 7) % 9) / 1000)
    return {"session": session, "conflicts": conflicts}


class _RunLeaseKeeper:
    """Renew simulated worker leases while a burst is being claimed, measured and completed."""

    def __init__(self, client, headers: dict[str, dict[str, str]], errors: list[str], max_parallel: int,
                 heartbeat_client=None, interval_s: float = 10.0) -> None:
        self.client = client
        # Separate TestClient portals keep heartbeats moving while the worker and
        # console clients are busy with their own concurrent requests.
        self.heartbeat_client = heartbeat_client or client
        self.headers = headers
        self.errors = errors
        self.max_parallel = max(1, max_parallel)
        self.interval_s = interval_s
        self._slots = Condition()
        self._active_requests = 0
        self._waiting_heartbeats = 0
        self._lock = Lock()
        self._runs: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
        self._run_locks: dict[str, Lock] = {}
        self._stop = Event()
        self._thread = Thread(target=self._heartbeat_loop, name="scale-run-heartbeats", daemon=True)
        self._thread.start()

    def request(self, method: str, path: str, **kwargs):
        return self._request(self.client, method, path, **kwargs)

    def _request(self, client, method: str, path: str, **kwargs):
        heartbeat = path.startswith("/v1/runs/") and path.endswith("/heartbeat")
        with self._slots:
            if heartbeat:
                self._waiting_heartbeats += 1
            while (self._active_requests >= self.max_parallel
                   or (not heartbeat and self._waiting_heartbeats > 0)):
                self._slots.wait()
            if heartbeat:
                self._waiting_heartbeats -= 1
            self._active_requests += 1
        try:
            return getattr(client, method)(path, **kwargs)
        finally:
            with self._slots:
                self._active_requests -= 1
                self._slots.notify_all()

    def add(self, session: dict[str, str], claim: dict[str, Any]) -> None:
        with self._lock:
            run_id = str(claim["run_id"])
            self._runs[run_id] = (session, claim)
            self._run_locks[run_id] = Lock()

    def complete(self, run_id: str) -> None:
        with self._lock:
            run_id = str(run_id)
            run_lock = self._run_locks.get(run_id)
        if run_lock is None:
            return
        # Drain a renewal already on the wire before the caller reports and releases the run. Otherwise that
        # renewal can arrive after release, get the expected 409, and be misreported as a lost lease.
        with run_lock:
            with self._lock:
                self._runs.pop(run_id, None)
                self._run_locks.pop(run_id, None)

    def _heartbeat(self, run_id: str, session: dict[str, str], claim: dict[str, Any]) -> None:
        with self._lock:
            run_lock = self._run_locks.get(run_id)
        if run_lock is None:
            return
        with run_lock:
            with self._lock:
                if run_id not in self._runs:
                    return
            self._heartbeat_active(run_id, session, claim)

    def _heartbeat_active(self, run_id: str, session: dict[str, str], claim: dict[str, Any]) -> None:
        path = f"/v1/runs/{run_id}/heartbeat"
        try:
            response = self._request(self.heartbeat_client, "post", path,
                                     json={"fence": claim["fence"], "progress": {"phase": "scale-proof"}},
                                     headers=self.headers[session["node"]])
        except Exception as exc:  # noqa: BLE001 - keep the run report actionable if a heartbeat fails
            with self._lock:
                if run_id in self._runs:
                    self.errors.append(f"{path} raised {type(exc).__name__}")
            return
        if response.status_code != 200:
            with self._lock:
                # A completion may release the run while this heartbeat was in flight.
                if run_id in self._runs:
                    self.errors.append(f"{path} answered HTTP {response.status_code}")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            with self._lock:
                snapshot = list(self._runs.items())
            if not snapshot:
                continue
            with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(snapshot)),
                                    thread_name_prefix="scale-run-heartbeat") as pool:
                futures = [pool.submit(self._heartbeat, run_id, session, claim)
                           for run_id, (session, claim) in snapshot]
                for future in futures:
                    future.result()

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


def _sample_page(client, page: str, data: tuple[str, ...], samples: int,
                 gate: Barrier | None = None, request_pool=None, forwarded_ip: str = "") -> dict[str, Any]:
    if gate is not None:
        gate.wait(timeout=30)
    latencies: list[float] = []
    intervals: list[tuple[float, float]] = []
    errors: list[str] = []
    calls: dict[str, list[float]] = {}
    for _ in range(samples):
        started = time.perf_counter()
        paths = (page, *COMMON_CALLS, *data)
        if request_pool is None:
            results = [_timed(client, path, forwarded_ip) for path in paths]
        else:
            futures = [request_pool.submit(_timed, client, path, forwarded_ip) for path in paths]
            results = [future.result() for future in futures]
        for path, (took, status, detail) in zip(paths, results):
            calls.setdefault(path, []).append(took)
            if status != 200:
                suffix = f": {detail}" if detail else ""
                errors.append(f"{path} answered HTTP {status}{suffix}")
        ended = time.perf_counter()
        # Independent browser fetches complete together; view readiness is the batch's wall-clock duration.
        latencies.append(ended - started)
        intervals.append((started, ended))
    return {"page": page, "latencies": latencies, "intervals": intervals, "errors": errors,
            "calls": {path: round(percentile(values, 95), 3) for path, values in calls.items()}}


def _sample_picker(client, samples: int, gate: Barrier | None = None, request_pool=None,
                   forwarded_ip: str = "") -> dict[str, Any]:
    if gate is not None:
        gate.wait(timeout=30)
    latencies: list[float] = []
    intervals: list[tuple[float, float]] = []
    errors: list[str] = []
    for _ in range(samples):
        started = time.perf_counter()
        if request_pool is None:
            took, status, detail = _timed(client, PICKER, forwarded_ip)
        else:
            took, status, detail = request_pool.submit(_timed, client, PICKER, forwarded_ip).result()
        ended = time.perf_counter()
        latencies.append(took * 1000)
        intervals.append((started, ended))
        if status != 200:
            suffix = f": {detail}" if detail else ""
            errors.append(f"{PICKER} answered HTTP {status}{suffix}")
    return {"latencies": latencies, "intervals": intervals, "errors": errors}


def _claim_round(client, sessions: list[dict[str, str]], headers: dict[str, dict[str, str]], sc: Scenario,
                 measure: bool = False, worker_request: Callable | None = None,
                 on_claim: Callable | None = None, measurement_client=None) -> tuple[list[dict[str, Any]], int, list[tuple[float, float]],
                                                 list[dict[str, Any]], dict[str, Any] | None]:
    claim_workers = min(sc.claim_concurrency, len(sessions))
    if claim_workers < 1:
        return [], 0, [], [], None
    measure_workers = min(sc.measurement_concurrency, len(PAGES) + 1) if measure else 0
    gate = Barrier(claim_workers + measure_workers + 1) if measure_workers else None
    page_results: list[dict[str, Any]] = []
    picker_result = None
    measurement_client = measurement_client or client
    with ThreadPoolExecutor(max_workers=claim_workers, thread_name_prefix="scale-worker") as claims_pool:
        # The bounded pool queues 200 sessions behind the configured request slots. Interleave that queue by node so the
        # first in-flight burst represents the fleet rather than six machines repeated four times each.
        claim_futures = [claims_pool.submit(_claim_session, client, session, headers,
                                            gate if gate is not None and i < claim_workers else None,
                                            worker_request, on_claim, max(4, sc.claim_concurrency))
                         for i, session in enumerate(fleet_poll_order(sessions))]
        if measure_workers:
            measure_jobs = [(page, data) for page, data in PAGES.items()]
            # Model a small group of browser clients, each fanning out up to six API calls. A shared
            # pool sized only to the number of views serializes each page behind unrelated clients.
            with ThreadPoolExecutor(max_workers=measure_workers * CONSOLE_REQUESTS_PER_VIEW,
                                    thread_name_prefix="scale-console-http") as request_pool:
                with ThreadPoolExecutor(max_workers=measure_workers, thread_name_prefix="scale-console") as views_pool:
                    page_futures = [views_pool.submit(_sample_page, measurement_client, page, data, sc.page_samples,
                                                      gate if i < measure_workers else None, request_pool,
                                                      f"198.51.100.{i + 1}")
                                    for i, (page, data) in enumerate(measure_jobs)]
                    picker_future = views_pool.submit(_sample_picker, measurement_client, sc.picker_samples,
                                                      request_pool=request_pool, forwarded_ip="198.51.100.254")
                    gate.wait(timeout=30)
                    claim_results = [future.result() for future in claim_futures]
                    page_results = [future.result() for future in page_futures]
                    picker_result = picker_future.result()
        else:
            claim_results = [future.result() for future in claim_futures]
    successes = [result for result in claim_results if result.get("claim")]
    intervals = [result["interval"] for result in successes]
    conflicts = sum(result.get("conflicts", 0) for result in claim_results)
    return claim_results, conflicts, intervals, page_results, picker_result


def _without_local_embeddings(function):
    """Keep local vector-model inference out of the control-plane request budget."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        from unittest.mock import patch

        with patch("aiflow.v4.control.knowledge._embedding", return_value=None):
            return function(*args, **kwargs)
    return wrapped


@_without_local_embeddings
def run(db, leases, workdir: Path, sc: Scenario = Scenario(), release: str = "", jira: Any = None,
        budgets: tuple[Budget, ...] = BUDGETS, log: Callable[[str], None] = lambda _m: None) -> tuple[dict, Any]:
    """Run the scenario once. Returns (report, ctx); `report` is finalized against `budgets`."""
    from fastapi.testclient import TestClient
    from .api import create_app
    from .console_auth import COOKIE_NAME, csrf_token, issue_session
    from .ui import mount_ui
    started = time.perf_counter()
    ctx = build_context(db, leases, workdir, sc, jira)
    app = create_app(ctx)
    mount_ui(app, ctx)
    request_activity = {"active_workers": 0, "active_console": 0, "peak_inflight": 0,
                        "worker_console_overlap": False}
    activity_lock = Lock()
    worker_route_latencies: dict[str, list[float]] = {"/v1/work/candidates": [], "/v1/work/claim": []}
    console_paths = {path.split("?", 1)[0] for page, data in PAGES.items()
                     for path in (page, *COMMON_CALLS, *data)} | {PICKER}

    @app.middleware("http")
    async def track_scale_overlap(request, call_next):
        path = request.url.path
        request_started = time.perf_counter()
        worker_request = (path in {"/v1/work/candidates", "/v1/work/claim"}
                          or (path.startswith("/v1/runs/") and path.endswith("/heartbeat")))
        console_request = path in console_paths or path.startswith(("/v1/business/", "/v1/stakeholder/",
                                                                      "/v1/projects/"))
        with activity_lock:
            if worker_request:
                request_activity["active_workers"] += 1
            if console_request:
                request_activity["active_console"] += 1
            inflight = request_activity["active_workers"] + request_activity["active_console"]
            request_activity["peak_inflight"] = max(request_activity["peak_inflight"], inflight)
            if request_activity["active_workers"] and request_activity["active_console"]:
                request_activity["worker_console_overlap"] = True
        try:
            return await call_next(request)
        finally:
            with activity_lock:
                if path in worker_route_latencies:
                    worker_route_latencies[path].append((time.perf_counter() - request_started) * 1000)
                if worker_request:
                    request_activity["active_workers"] -= 1
                if console_request:
                    request_activity["active_console"] -= 1

    # Keep the production per-IP rate limit active while representing distinct nodes and browser clients.
    # TestClient otherwise puts the entire simulated 200-session fleet behind one synthetic IP.
    ctx.settings.trusted_proxies = ["127.0.0.1/32"]
    client = TestClient(app, client=("127.0.0.1", 50001))
    heartbeat_client = TestClient(app, client=("127.0.0.1", 50002))
    measurement_client = TestClient(app, client=("127.0.0.1", 50003))
    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "scale-proof-console-session-secret-at-least-32-bytes"
    ctx.settings.owner_token = "scale-proof-owner-token"
    db.run("INSERT INTO console_users (email, display_name, role, roles, password_hash, totp_enabled)"
           " VALUES (%s,%s,%s,%s,%s,true) ON CONFLICT (email) DO NOTHING",
           ("scale-proof@example.test", "Scale proof", "administrator", ["administrator"],
            "scale-proof-password-hash"))
    console_session = issue_session("scale-proof@example.test", "administrator", 2_000_000_000,
                                    ctx.settings.console_session_secret)
    console_headers = {"X-CSRF-Token": csrf_token(console_session, ctx.settings.console_session_secret),
                       "X-Forwarded-For": "198.51.100.200"}
    for api_client in (client, heartbeat_client, measurement_client):
        api_client.cookies.set(COOKIE_NAME, console_session)
    _activate_business_license(ctx)
    app.state.sync_accounts()
    pl = plan(sc)
    sessions = sessions_of(sc)
    hosts = serving(sc)
    errors: list[str] = []

    headers: dict[str, dict[str, str]] = {}
    for node_index, name in enumerate(node_names(sc), start=1):
        project = next(p for p, nodes in hosts.items() if name in nodes)
        issued = client.post("/v1/nodes/join-tokens", json={"provision_key": JOIN_KEY, "name": name,
                                                             "role": "worker", "project": project},
                             headers=console_headers)
        if issued.status_code != 200:
            detail = issued.json().get("detail", "") if issued.headers.get("content-type", "").startswith("application/json") else ""
            errors.append(f"node {name} join token answered HTTP {issued.status_code}: {detail}")
            continue
        joined = client.post("/v1/nodes/join", json={"join_token": issued.json().get("token", "")},
                             headers=console_headers)
        if joined.status_code != 200:
            errors.append(f"node {name} could not join: HTTP {joined.status_code}")
            continue
        headers[name] = {"Authorization": f"Bearer {joined.json()['node_token']}",
                         "X-Forwarded-For": f"192.0.2.{node_index}"}
        accounts = ctx.repo().accounts_on(name)
        heartbeat = client.post(f"/v1/nodes/{name}/heartbeat", json={
            "slots_total": max(1, sc.sessions // sc.machines),
            # Four worker slots on a five-core host leave enough CPU headroom for all 200 sessions at the low
            # simulated load; otherwise the 0.85 per-CPU limit caps every host at three slots before the burst.
            "metrics": {"hostname": name, "load1": 0.1, "cpus": 5,
                        "mem_total": 16 * 1024 ** 3, "mem_used": 4 * 1024 ** 3,
                        "disk_total": 100 * 1024 ** 3, "disk_free": 80 * 1024 ** 3},
            "tools": {"git": "git version 2.43.0", "uv": "uv 0.8.1", "codex": "codex 1.0.0",
                      "codex-code-mode-host": "codex-code-mode-host 1.0.0", "claude": "Claude Code 2.1.305",
                      "grok": "grok 1.0.0", "opencode": "opencode 1.0.0"},
            "system_conditions": [{"id": item, "result": "ready"} for item in (
                "disk-space", "workspace-permissions", "control-network", "repository-network")],
            "credential_checks": {account.id: {"present": True, "valid": True} for account in accounts},
        }, headers=headers[name])
        if heartbeat.status_code != 200:
            errors.append(f"node {name} heartbeat answered HTTP {heartbeat.status_code}")

    # Join and heartbeat checks run in background threads. Let them finish before the controlled readiness
    # snapshot below so a late join result cannot replace the simulated node's reported machine conditions.
    readiness_threads = {f"aiflow4-join-readiness-{name}" for name in headers}
    readiness_threads.update(f"aiflow4-heartbeat-readiness-{name}" for name in headers)
    readiness_deadline = time.monotonic() + 30
    while time.monotonic() < readiness_deadline:
        if not any(thread.name in readiness_threads for thread in threading.enumerate()):
            break
        time.sleep(0.01)
    else:
        errors.append("node readiness checks did not settle before the scale scenario")

    # The scenario begins load traffic only after every simulated node has completed its normal readiness gate.
    # This keeps the measurement about project/session scale rather than a race with asynchronous join probes.
    simulated_machine_conditions = [{"id": item, "result": "ready"} for item in (
        "disk-space", "workspace-permissions", "control-network", "repository-network")]
    with ThreadPoolExecutor(max_workers=min(sc.claim_concurrency, max(1, len(headers))),
                            thread_name_prefix="scale-readiness") as pool:
        refreshes = {
            pool.submit(system_check.refresh_node, ctx, name, "manual",
                        machine_conditions=simulated_machine_conditions,
                        credential_checks={account.id: {"present": True, "valid": True}
                                           for account in ctx.repo().accounts_on(name)}): name
            for name in headers
        }
        for future, name in ((future, refreshes[future]) for future in refreshes):
            try:
                readiness = future.result()
            except Exception as exc:  # noqa: BLE001 - include readiness failures in the benchmark report
                errors.append(f"node {name} readiness check failed: {type(exc).__name__}")
                continue
            accounts = [account.id for account in ctx.repo().accounts_on(name)]
            for project in (key for key, nodes in hosts.items() if name in nodes):
                blockers = system_check.blocking_reasons(readiness, project, accounts[0] if accounts else None,
                                                        accounts)
                if blockers:
                    errors.append(f"node {name} is not ready for {project}: "
                                  + ", ".join(f"{item.get('id')}={item.get('result')}" for item in blockers))
    last_node_heartbeat = time.perf_counter()
    _load_history(db, sc, pl, sessions)
    log(f"fleet ready: {len(headers)} machines, {len(sessions)} sessions, {len(pl['history'])} earlier tickets")

    per_node = max(1, sc.sessions // sc.machines)
    ready_at: dict[str, float] = {}
    ready_at_lock = Lock()
    pickup: dict[str, float] = {}
    dependents: dict[str, list[str]] = {}
    taken: set[str] = set()
    conflicts = rounds_total = peak_active = 0
    measured: dict[str, Any] = {}
    peak_checks: dict[str, tuple[bool, str]] = {}
    pickup_by_wave: dict[int, list[float]] = {}
    offset = 0

    def beat() -> None:  # node agents heartbeat every 15-30 s; the console shows a silent node as offline
        nonlocal last_node_heartbeat
        started = time.perf_counter()
        if started - last_node_heartbeat < 15:
            return
        workers = min(sc.claim_concurrency, len(headers))
        if workers < 1:
            return
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scale-heartbeat") as pool:
            heartbeats = [pool.submit(client.post, f"/v1/nodes/{name}/heartbeat",
                                      json={"slots_total": per_node}, headers=h) for name, h in headers.items()]
            for name, future in zip(headers, heartbeats):
                response = future.result()
                if response.status_code != 200:
                    errors.append(f"node {name} heartbeat answered HTTP {response.status_code}")
        last_node_heartbeat = time.perf_counter()

    def finish_one(session: dict, claim: dict, keeper: _RunLeaseKeeper) -> tuple[str, list[str]]:
        h, fence = headers[session["node"]], claim["fence"]
        run_id = claim["run_id"]
        key = claim["ticket"]["key"]
        issues = []
        try:
            heartbeat = keeper.request("post", f"/v1/runs/{run_id}/heartbeat",
                                       json={"fence": fence, "progress": {"phase": "ai"}}, headers=h)
            if heartbeat.status_code != 200:
                issues.append(f"/v1/runs/{run_id}/heartbeat answered HTTP {heartbeat.status_code}")
            keeper.complete(run_id)
            result_path = f"/v1/runs/{run_id}/result"
            result = keeper.request("post", result_path,
                                    json={"fence": fence, "outcome": "done", "summary": "simulated session"},
                                    headers=h)
            if result.status_code != 200:
                issues.append(f"{result_path} answered HTTP {result.status_code}")
            else:
                # The real worker transitions Jira before releasing its lease. Mirror the terminal status here
                # while this run still owns its lease, before another poll can see the ticket as To Do.
                db.run("UPDATE tickets SET status='Done', done_at=coalesce(done_at,now()), synced_at=now()"
                       " WHERE key=%s", (key,))
            release_path = f"/v1/runs/{run_id}/release"
            released = keeper.request("post", release_path, json={"fence": fence}, headers=h)
            if released.status_code != 200:
                issues.append(f"{release_path} answered HTTP {released.status_code}")
        finally:
            keeper.complete(run_id)
        if not issues:
            # The next poll pass can run while completions are still draining. Publish a dependent ticket's
            # ready time as soon as its blocker is done so that early claim is measured from the real transition.
            with ready_at_lock:
                for dep in dependents.get(key, []):
                    ready_at[dep] = time.perf_counter()
        return key, issues

    for wave_no, wave in enumerate(pl["waves"]):
        _insert_wave(db, wave, offset)
        offset += len(wave)
        now = time.perf_counter()
        for t in wave:
            if not t["held"] and not t["blocked_by"]:
                ready_at[t["key"]] = now
            for blocker_key in t["blocked_by"]:
                dependents.setdefault(blocker_key, []).append(t["key"])
        pickable = {t["key"] for t in wave if not t["held"]}
        for rounds in range(1, sc.max_rounds + 1):
            beat()
            first_burst = wave_no == 0 and rounds == 1
            keeper = _RunLeaseKeeper(client, headers, errors, sc.claim_concurrency,
                                     heartbeat_client=heartbeat_client)
            try:
                claim_results, round_conflicts, claim_intervals, page_results, picker_result = _claim_round(
                    client, sessions, headers, sc, measure=first_burst, worker_request=keeper.request,
                    on_claim=keeper.add, measurement_client=measurement_client)
                conflicts += round_conflicts
                live: list[tuple[dict, dict]] = []
                idle_sessions: list[dict[str, str]] = []
                for result in claim_results:
                    if result.get("error"):
                        errors.append(result["error"])
                        continue
                    if not result.get("claim"):
                        idle_sessions.append(result["session"])
                        continue
                    session, claim = result["session"], result["claim"]
                    key = claim["ticket"]["key"]
                    with ready_at_lock:
                        if key in ready_at and key not in pickup:
                            elapsed = result["claimed_at"] - ready_at[key]
                            pickup[key] = elapsed
                            pickup_by_wave.setdefault(wave_no + 1, []).append(elapsed)
                    live.append((session, claim))
                if not live:
                    break
                rounds_total += 1
                if first_burst:
                    peak_active = len(live)
                    with activity_lock:
                        observed_activity = dict(request_activity)
                    measured, peak_checks = _measure_peak(client, ctx, sc, live, errors, page_results, picker_result,
                                                          claim_intervals, observed_activity)
                    log(f"peak: {peak_active} sessions working")
                # Keep completion traffic bounded so it cannot occupy every worker request slot while the
                # independent lease heartbeats run. The first burst also lets idle workers poll again while runs
                # finish instead of making ready tickets wait for the whole burst to drain.
                with ThreadPoolExecutor(max_workers=min(8, len(live)),
                                        thread_name_prefix="scale-complete") as pool:
                    completed = [pool.submit(finish_one, session, claim, keeper) for session, claim in live]
                    if first_burst and idle_sessions:
                        followup_results, followup_conflicts, _, _, _ = _claim_round(
                            client, sessions, headers, sc, worker_request=keeper.request,
                            on_claim=keeper.add, measurement_client=measurement_client)
                        conflicts += followup_conflicts
                        followup_live = []
                        for result in followup_results:
                            if result.get("error"):
                                errors.append(result["error"])
                                continue
                            if not result.get("claim"):
                                continue
                            session, claim = result["session"], result["claim"]
                            key = claim["ticket"]["key"]
                            with ready_at_lock:
                                if key in ready_at and key not in pickup:
                                    elapsed = result["claimed_at"] - ready_at[key]
                                    pickup[key] = elapsed
                                    pickup_by_wave.setdefault(wave_no + 1, []).append(elapsed)
                            followup_live.append((session, claim))
                        if followup_live:
                            rounds_total += 1
                            completed.extend(pool.submit(finish_one, session, claim, keeper)
                                             for session, claim in followup_live)
                    for future in completed:
                        key, finish_errors = future.result()
                        errors.extend(finish_errors)
                        if finish_errors:
                            continue
                        taken.add(key)
                        with ready_at_lock:
                            for dep in dependents.get(key, []):
                                ready_at.setdefault(dep, time.perf_counter())
            finally:
                keeper.close()
            if pickable <= taken:
                break
        log(f"wave {wave_no + 1}: {len(pickup)} tickets picked up so far")

    beat()
    checks = _checks(client, ctx, sc, pl, sessions, hosts, headers, peak_checks, pickup)
    traffic = [t for wave in pl["waves"] for t in wave]
    measured["ticket_pickup"] = {
        "value": round(percentile(pickup.values(), 95), 3), "unit": "s", "statistic": "p95",
        "samples": len(pickup), "p50": round(percentile(pickup.values(), 50), 3),
        "max": round(max(pickup.values(), default=0.0), 3),
        "by_wave": {str(wave): {"p95": round(percentile(values, 95), 3), "p50": round(percentile(values, 50), 3),
                                 "max": round(max(values, default=0.0), 3), "samples": len(values)}
                    for wave, values in sorted(pickup_by_wave.items())}}
    report = {
        "schema": scale_proof.SCHEMA, "release": release, "scenario": asdict(sc), "plan_digest": pl["digest"],
        "environment": {"python": platform.python_version(), "platform": platform.system().lower(),
                        "cpus": os.cpu_count(), "api_execution": "FastAPI TestClient in-process",
                        "persistence": "PostgreSQL and Redis", "simulated_workers": True,
                        "simulated_external_integrations": ["GitHub App", "Jira"],
                        "external_ai_providers_called": False,
                        "local_embeddings_enabled": False,
                        "max_parallel_worker_requests": sc.claim_concurrency,
                        "max_parallel_console_views": min(sc.measurement_concurrency, len(PAGES) + 1),
                        "max_parallel_requests": sc.claim_concurrency +
                        min(sc.measurement_concurrency, len(PAGES) + 1) * CONSOLE_REQUESTS_PER_VIEW},
        "counts": {"projects": len(project_keys(sc)), "machines": len(headers),
                   "accounts": len({s["id"] for s in sessions}),
                   "sessions": len(sessions), "history_tickets": len(pl["history"]),
                   "traffic_tickets": len(traffic), "held_tickets": sum(t["held"] for t in traffic),
                   "blocked_tickets": sum(bool(t["blocked_by"]) for t in traffic), "pickups": len(pickup)},
        # how the claims fall into rounds is for information; the comparable counts are the ones above
        "observed": {"peak_active_runs": peak_active, "rounds": rounds_total, "claim_conflicts": conflicts,
                     "peak_inflight_app_requests": request_activity["peak_inflight"],
                     "worker_route_p95_ms": {path: round(percentile(values, 95), 3)
                                              for path, values in worker_route_latencies.items()}},
        "measurements": measured, "checks": checks,
        "errors": errors, "duration_s": round(time.perf_counter() - started, 1)}
    return scale_proof.finalize(report, budgets), ctx


def _measure_peak(client, ctx, sc: Scenario, live: list, errors: list[str],
                  page_results: list[dict[str, Any]], picker_result: dict[str, Any] | None,
                  claim_intervals: list[tuple[float, float]],
                  request_activity: dict[str, Any]) -> tuple[dict, dict]:
    pages: dict[str, dict[str, Any]] = {}
    view_intervals: list[tuple[float, float]] = []
    for result in page_results:
        page = result["page"]
        samples = result["latencies"]
        errors.extend(result["errors"])
        view_intervals.extend(result["intervals"])
        pages[page] = {"p95": round(percentile(samples, 95), 3), "p50": round(percentile(samples, 50), 3),
                       "max": round(max(samples, default=0.0), 3), "samples": len(samples),
                       "calls_p95": result.get("calls", {})}
    picker_result = picker_result or {"latencies": [], "intervals": [], "errors": []}
    picker = picker_result["latencies"]
    errors.extend(picker_result["errors"])
    view_intervals.extend(picker_result["intervals"])
    client_overlap = any(claim_start < view_end and view_start < claim_end
                         for claim_start, claim_end in claim_intervals
                         for view_start, view_end in view_intervals)
    overlap = client_overlap and bool(request_activity.get("worker_console_overlap"))
    measured = {
        "console_page_load": {"value": max((p["p95"] for p in pages.values()), default=0.0), "unit": "s",
                              "statistic": "p95 per view, worst view", "views": pages,
                              "slowest_view": max(pages, key=lambda p: pages[p]["p95"]) if pages else ""},
        "project_picker": {"value": round(percentile(picker, 95), 3), "unit": "ms", "statistic": "p95",
                           "samples": len(picker), "p50": round(percentile(picker, 50), 3),
                           "max": round(max(picker, default=0.0), 3)}}
    state_response = client.get("/v1/state", headers={"Authorization": f"Bearer {ctx.settings.owner_token}"})
    state = state_response.json()
    active_runs = state.get("active_runs") if isinstance(state, dict) else None
    active_by_node = ctx.db.all("SELECT node,count(*) AS n FROM runs WHERE status='running'"
                                " GROUP BY node ORDER BY node")
    active_counts = [int(row["n"]) for row in active_by_node]
    active_detail = (f"{len(active_runs)} active runs for {len(live)} working sessions; "
                     f"{len(active_by_node)} machines have active runs, per-machine count "
                     f"{min(active_counts, default=0)}–{max(active_counts, default=0)}"
                     if isinstance(active_runs, list) else f"state answered HTTP {state_response.status_code}: {state}")
    checks = {"active_runs_visible": (isinstance(active_runs, list) and len(active_runs) == len(live), active_detail)}
    checks["measurement_overlap"] = (overlap,
                                      f"console and picker requests {'overlapped' if overlap else 'did not overlap'} "
                                      f"worker requests inside the app (peak {request_activity.get('peak_inflight', 0)} in flight)")
    checks["measurement_active_threshold"] = (len(live) >= sc.measurement_min_active,
                                               f"{len(live)} sessions were active while measuring; expected at least "
                                               f"{sc.measurement_min_active}; {len(active_by_node)} of "
                                               f"{sc.machines} machines were active, per-machine counts "
                                               f"{ {count: active_counts.count(count) for count in sorted(set(active_counts))} }")
    key = live[0][1]["ticket"]["key"]
    intruder = ctx.leases.claim(key, "intruder-run", 1000)
    checks["lease_exclusive"] = (not intruder.ok, f"a second claim of {key} while it is leased "
                                 + ("was refused" if not intruder.ok else "was granted"))
    return measured, checks


def _checks(client, ctx, sc, pl, sessions, hosts, headers, peak_checks, pickup) -> list[dict[str, Any]]:
    db = ctx.db
    out = [{"name": n, "ok": ok, "detail": d} for n, (ok, d) in peak_checks.items()]

    def check(name: str, ok: bool, detail: str) -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    traffic = [t for wave in pl["waves"] for t in wave]
    keys = [t["key"] for t in traffic]
    runs = db.all("SELECT ticket_key, node, fence, status, started_at, ended_at, project FROM runs"
                  " WHERE ticket_key = ANY(%s)", (keys,))
    per_ticket: dict[str, list[dict]] = {}
    for r in runs:
        per_ticket.setdefault(r["ticket_key"], []).append(r)
    expected = [t["key"] for t in traffic if not t["held"]]
    missing = [k for k in expected if k not in per_ticket]
    twice = [k for k, rs in per_ticket.items() if len(rs) > 1]
    duplicate_detail = ", ".join(f"{key} ({len(per_ticket[key])} runs)" for key in twice[:5])
    check("every_ready_ticket_picked_once", not missing and not twice,
          f"{len(expected) - len(missing)} of {len(expected)} ready tickets picked, {len(twice)} picked more than once"
          + (f" (not picked: {', '.join(missing[:5])})" if missing else "")
          + (f" (duplicates: {duplicate_detail})" if duplicate_detail else ""))
    held = [t["key"] for t in traffic if t["held"] and t["key"] in per_ticket]
    check("held_tickets_not_picked", not held, f"{len(held)} held tickets were picked" + (f": {', '.join(held[:5])}" if held else ""))
    late = []
    for t in traffic:
        for b in t["blocked_by"]:
            mine, theirs = per_ticket.get(t["key"]), per_ticket.get(b)
            if mine and theirs and mine[0]["started_at"] < theirs[0]["ended_at"]:
                late.append(t["key"])
    check("blocked_tickets_wait_for_blocker", not late, f"{len(late)} blocked tickets started before their blocker finished")
    wrong = [r["ticket_key"] for r in runs if r["node"] not in hosts[r["project"]]]
    check("claims_on_serving_machines", not wrong, f"{len(wrong)} claims ran on a machine that does not serve the project")
    state_response = client.get("/v1/state", headers={"Authorization": f"Bearer {ctx.settings.owner_token}"})
    state = state_response.json()
    active_runs = state.get("active_runs") if isinstance(state, dict) else None
    if isinstance(active_runs, list):
        check("no_run_left_working", not active_runs and not any(r["status"] == "running" for r in runs),
              f"{len(active_runs)} runs still working at the end")
    else:
        check("no_run_left_working", False,
              f"state answered HTTP {state_response.status_code}: {state}")
        active_runs = []
    sizes = (len(state.get("projects", [])), len([n for n in state.get("nodes", []) if n["status"] == "online"]),
             len(state.get("accounts", [])))
    visible_sessions = sum(int(account.get("session_limit") or 1) for account in state.get("accounts", []))
    invalid_fences = [r["ticket_key"] for r in runs if r.get("fence") is None or int(r["fence"]) < 1]
    first_fences = [key for key, attempts in per_ticket.items()
                    if int(min(attempts, key=lambda run: run["started_at"]).get("fence") or 0) != 1]
    check("one_fence_per_fresh_ticket", not invalid_fences and not first_fences,
          f"{len(invalid_fences)} runs had a non-positive fence; {len(first_fences)} fresh tickets did not start at fence 1")
    expected_accounts = len({s["id"] for s in sessions})
    check("fleet_fully_visible", sizes == (sc.projects, sc.machines, expected_accounts)
          and visible_sessions == len(sessions),
          f"state shows {sizes[0]} projects, {sizes[1]} online machines, {sizes[2]} licensed accounts and "
          f"{visible_sessions} concurrent sessions (expected {sc.projects}, {sc.machines}, "
          f"{expected_accounts} accounts, {len(sessions)} sessions)")
    picker_response = client.get(PICKER)
    picker = picker_response.json().get("projects", []) if picker_response.status_code == 200 else []
    check("picker_lists_every_project", len(picker) == sc.projects, f"the picker lists {len(picker)} of {sc.projects} projects")
    return out


# --- an isolated run for CI and developers ----------------------------------------------------------------------
@contextmanager
def scratch(database_url: str, redis_url: str) -> Iterator[tuple[Any, Any]]:
    """Use one isolated schema and Redis prefix inside an explicitly dedicated test/worktree namespace."""
    import re
    import psycopg
    import redis
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from ..core.fencing import Leases
    from .db import DB

    local = os.environ.get("GITHUB_ACTIONS") != "true"
    database = str(conninfo_to_dict(database_url).get("dbname") or "")
    if not database:
        raise ValueError("scale proof needs a named PostgreSQL database")
    if local and not (database.endswith("_test") or re.search(r"_wt[a-z0-9]+$", database)):
        raise ValueError("scale proof requires a dedicated *_test or *_wt<id> database")
    redis_db = redis_url.rsplit("/", 1)[-1].split("?", 1)[0]
    redis_base = os.environ.get("AIFLOW4_TEST_REDIS_PREFIX", os.environ.get("REDIS_PREFIX", ""))
    if local and (redis_db in {"", "0", "5"} or not (
            redis_base.endswith("_test:") or re.search(r"_wt[a-z0-9]+:$", redis_base))):
        raise ValueError("scale proof requires a dedicated non-live Redis index and a *_test: or *_wt<id>: prefix")

    schema = "scaleproof_" + uuid.uuid4().hex
    admin = psycopg.connect(database_url, autocommit=True)
    # Migration 005 creates pgvector. Keep the extension in `public`, which is shared by later test fixtures;
    # otherwise the first scratch-schema run installs it in its own schema and dropping that schema leaves the
    # extension catalog entry behind without its `vector` type.
    admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = DB(make_conninfo(database_url, options=f"-c search_path={schema},public"), max_size=40)
    redis_prefix = f"{redis_base}scaleproof:{uuid.uuid4().hex}:"
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    try:
        db.migrate()
        yield db, Leases(r, redis_prefix)
    finally:
        db.close()
        for key in r.scan_iter(match=redis_prefix + "*"):
            r.delete(key)
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        admin.close()
