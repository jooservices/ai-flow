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
     per round; at the busiest moment of the first wave (all sessions working) it measures the console pages and
     the project picker; the pickup time of every ticket is measured from "ready" to "claim granted";
  3. checks correct behaviour at this size (each ticket picked exactly once, held tickets untouched, blocked
     tickets after their blocker, leases exclusive, fleet and projects fully visible);
  4. evaluates the budgets of `scale_proof` and returns the report.

Docs: docs/v4/scale-proof.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import scale_proof
from .scale_proof import BUDGETS, Budget, percentile

PROVIDERS = ("codex", "claude", "opencode", "grok")
PRIORITIES = ("Highest", "High", "Medium", "Low")
HISTORY_STATUSES = (("Done", 70), ("In Progress", 10), ("In Verify", 10), ("Needs owner", 5))
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
# the console pages and the data calls each renders from (see ui/console.html); /integrations needs a live Jira
# and GitHub connection and is not part of the scenario
PAGES: dict[str, tuple[str, ...]] = {
    "/cc": ("/v1/state",), "/delivery": ("/v1/state",), "/sa": ("/v1/state",), "/lead": ("/v1/state",),
    "/business": ("/v1/state",), "/review": ("/v1/state", "/v1/reviews"), "/stakeholder": ("/v1/state",),
    "/knowledge": ("/v1/knowledge/resources",), "/projects": ("/v1/projects",),
}
PICKER = "/v1/projects"
JOIN_KEY = "scale-proof-join"


@dataclass(frozen=True)
class Scenario:
    projects: int = 100
    machines: int = 50
    sessions: int = 200
    wave_sizes: tuple[int, ...] = (200, 80, 80)  # tickets that become ready together: a morning burst, then trickles
    history_per_project: int = 20
    seed: int = 236
    page_samples: int = 5
    picker_samples: int = 20
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
    per = max(1, sc.sessions // sc.machines)
    out = []
    for n, node in enumerate(node_names(sc)):
        for s in range(per):
            provider = PROVIDERS[s % len(PROVIDERS)]
            out.append({"id": f"scale-{provider}-{n * per + s + 1:03d}", "provider": provider, "node": node})
    return out


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
    for size in sc.wave_sizes:
        tickets: list[dict[str, Any]] = []
        for _ in range(size):
            k = rng.choices(keys, weights)[0]
            counter[k] += 1
            t = {"key": f"{k}-{counter[k]}", "project": k, "priority": rng.choices(PRIORITIES, (5, 20, 60, 15))[0],
                 "held": False, "blocked_by": []}
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
    accounts = "accounts:\n" + "".join(
        f"  - {{id: {s['id']}, provider: {s['provider']}, node: {s['node']}, tier: middle, roles: [dev]}}\n"
        for s in sessions_of(sc))
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


def build_context(db, leases, root: Path, sc: Scenario, jira: Any = None):
    from ..core.controlrepo import ControlRepo
    from ..core.settings import Settings
    from .api import Ctx
    from .events import Events
    write_control_repo(root, sc)
    snap = ControlRepo.load(root)
    settings = Settings(join_token=JOIN_KEY, join_control_url="https://control.scale.test", reviewer_token="x" * 16,
                        owner_token="owner-scale-proof", console_session_secret="scale-proof-console-session-secret",
                        lease_ms=90_000, env="scale-proof")
    return Ctx(settings=settings, db=db, leases=leases, events=Events(db), repo=lambda: snap,
               jira=jira if jira is not None else SimJira())


def _load_history(db, sc: Scenario, pl: dict[str, Any], sessions: list[dict[str, str]]) -> None:
    hosts = serving(sc)
    by_node = {s["node"]: s for s in sessions}
    with db.pool.connection() as c:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO tickets (key, project, status, labels, summary, description, priority, jira_created)"
                " VALUES (%s,%s,%s,%s,%s,%s,'Medium',%s)",
                [(t["key"], t["project"], t["status"], [], f"Earlier work {t['key']}", "Acceptance criteria:\n- AC1: ok",
                  EPOCH + timedelta(seconds=i)) for i, t in enumerate(pl["history"])])
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
def _timed(client, path: str) -> tuple[float, int]:
    start = time.perf_counter()
    r = client.get(path)
    return time.perf_counter() - start, r.status_code


def run(db, leases, workdir: Path, sc: Scenario = Scenario(), release: str = "", jira: Any = None,
        budgets: tuple[Budget, ...] = BUDGETS, log: Callable[[str], None] = lambda _m: None) -> tuple[dict, Any]:
    """Run the scenario once. Returns (report, ctx); `report` is finalized against `budgets`."""
    from fastapi.testclient import TestClient
    from .api import create_app
    from .ui import mount_ui
    started = time.perf_counter()
    ctx = build_context(db, leases, workdir, sc, jira)
    app = create_app(ctx)
    mount_ui(app, ctx)
    client = TestClient(app)  # in-process: the same routes, auth and database as a node or a browser reaches
    # Console page measurements must use the same authenticated read paths as a real console user. Develop gates
    # the Knowledge data API by role, so give this simulated console an owner session.
    from .console_auth import COOKIE_NAME, issue_session
    client.cookies.set(COOKIE_NAME, issue_session("scale-proof", "owner", int(time.time()) + 3600,
                                                  ctx.settings.console_session_secret))
    app.state.sync_accounts()
    pl = plan(sc)
    sessions = sessions_of(sc)
    hosts = serving(sc)
    errors: list[str] = []

    headers: dict[str, dict[str, str]] = {}
    for name in node_names(sc):
        project = next(p for p, nodes in hosts.items() if name in nodes)
        issued = client.post("/v1/nodes/join-tokens", json={"provision_key": JOIN_KEY, "name": name,
                                                             "role": "worker", "project": project})
        joined = client.post("/v1/nodes/join", json={"join_token": issued.json().get("token", "")})
        if joined.status_code != 200:
            errors.append(f"node {name} could not join: HTTP {joined.status_code}")
            continue
        headers[name] = {"Authorization": f"Bearer {joined.json()['node_token']}"}
        client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": max(1, sc.sessions // sc.machines)},
                    headers=headers[name])
    _load_history(db, sc, pl, sessions)
    log(f"fleet ready: {len(headers)} machines, {len(sessions)} sessions, {len(pl['history'])} earlier tickets")

    per_node = max(1, sc.sessions // sc.machines)
    ready_at: dict[str, float] = {}
    pickup: dict[str, float] = {}
    dependents: dict[str, list[str]] = {}
    taken: set[str] = set()
    conflicts = rounds_total = peak_active = 0
    measured: dict[str, Any] = {}
    peak_checks: dict[str, tuple[bool, str]] = {}
    offset = 0

    def beat() -> None:  # node agents heartbeat every 15-30 s; the console shows a silent node as offline
        for name, h in headers.items():
            client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": per_node}, headers=h)

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
            live: list[tuple[dict, dict]] = []
            for s in sessions:  # every session looks for work like a node worker: candidates, then claim
                h = headers.get(s["node"])
                cands = client.get("/v1/work/candidates", params={"account": s["id"]}, headers=h).json().get("tickets")
                if not cands:
                    continue
                r = client.post("/v1/work/claim", json={"ticket": cands[0]["key"], "account": s["id"],
                                                         "step": cands[0]["step"]}, headers=h)
                if r.status_code != 200:
                    conflicts += 1
                    continue
                key = cands[0]["key"]
                if key in ready_at and key not in pickup:
                    pickup[key] = time.perf_counter() - ready_at[key]
                live.append((s, r.json()))
            if not live:
                break
            rounds_total += 1
            if wave_no == 0 and rounds == 1:
                peak_active = len(live)
                measured, peak_checks = _measure_peak(client, ctx, sc, live, errors)
                log(f"peak: {peak_active} sessions working")
            for s, claim in live:
                h, fence = headers[s["node"]], claim["fence"]
                client.post(f"/v1/runs/{claim['run_id']}/heartbeat", json={"fence": fence, "progress": {"phase": "ai"}},
                            headers=h)
                client.post(f"/v1/runs/{claim['run_id']}/result",
                            json={"fence": fence, "outcome": "done", "summary": "simulated session"}, headers=h)
                client.post(f"/v1/runs/{claim['run_id']}/release", json={"fence": fence}, headers=h)
                key = claim["ticket"]["key"]
                db.run("UPDATE tickets SET status='Done' WHERE key=%s", (key,))  # what Jira sync mirrors back
                taken.add(key)
                for dep in dependents.get(key, []):
                    ready_at[dep] = time.perf_counter()  # a blocked ticket is ready once its blocker is Done
            if pickable <= taken:
                break
        log(f"wave {wave_no + 1}: {len(pickup)} tickets picked up so far")

    beat()
    checks = _checks(client, ctx, sc, pl, sessions, hosts, headers, peak_checks, pickup)
    traffic = [t for wave in pl["waves"] for t in wave]
    measured["ticket_pickup"] = {
        "value": round(percentile(pickup.values(), 95), 3), "unit": "s", "statistic": "p95",
        "samples": len(pickup), "p50": round(percentile(pickup.values(), 50), 3),
        "max": round(max(pickup.values(), default=0.0), 3)}
    report = {
        "schema": scale_proof.SCHEMA, "release": release, "scenario": asdict(sc), "plan_digest": pl["digest"],
        "environment": {"python": platform.python_version(), "platform": platform.system().lower(),
                        "cpus": os.cpu_count()},
        "counts": {"projects": len(project_keys(sc)), "machines": len(headers),
                   "sessions": len(sessions), "history_tickets": len(pl["history"]),
                   "traffic_tickets": len(traffic), "held_tickets": sum(t["held"] for t in traffic),
                   "blocked_tickets": sum(bool(t["blocked_by"]) for t in traffic), "pickups": len(pickup)},
        # how the claims fall into rounds is for information; the comparable counts are the ones above
        "observed": {"peak_active_runs": peak_active, "rounds": rounds_total, "claim_conflicts": conflicts},
        "measurements": measured, "checks": checks,
        "errors": errors, "duration_s": round(time.perf_counter() - started, 1)}
    return scale_proof.finalize(report, budgets), ctx


def _measure_peak(client, ctx, sc: Scenario, live: list, errors: list[str]) -> tuple[dict, dict]:
    pages: dict[str, dict[str, Any]] = {}
    for page, data in PAGES.items():
        samples = []
        for _ in range(sc.page_samples):
            total = 0.0
            for path in (page, *data):
                took, status = _timed(client, path)
                total += took
                if status != 200:
                    errors.append(f"{path} answered HTTP {status}")
            samples.append(total)
        pages[page] = {"p95": round(percentile(samples, 95), 3), "p50": round(percentile(samples, 50), 3),
                       "max": round(max(samples), 3), "samples": len(samples)}
    picker = []
    for _ in range(sc.picker_samples):
        took, status = _timed(client, PICKER)
        picker.append(took * 1000)
        if status != 200:
            errors.append(f"{PICKER} answered HTTP {status}")
    measured = {
        "console_page_load": {"value": max(p["p95"] for p in pages.values()), "unit": "s",
                              "statistic": "p95 per page, worst page", "pages": pages,
                              "slowest_page": max(pages, key=lambda p: pages[p]["p95"])},
        "project_picker": {"value": round(percentile(picker, 95), 3), "unit": "ms", "statistic": "p95",
                           "samples": len(picker), "p50": round(percentile(picker, 50), 3),
                           "max": round(max(picker), 3)}}
    state = client.get("/v1/state").json()
    checks = {"active_runs_visible": (len(state["active_runs"]) == len(live),
                                      f"state lists {len(state['active_runs'])} active runs for {len(live)} working sessions")}
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
    check("every_ready_ticket_picked_once", not missing and not twice,
          f"{len(expected) - len(missing)} of {len(expected)} ready tickets picked, {len(twice)} picked more than once"
          + (f" (not picked: {', '.join(missing[:5])})" if missing else ""))
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
    fences = [r["ticket_key"] for r in runs if r["fence"] != 1]
    check("one_fence_per_fresh_ticket", not fences, f"{len(fences)} fresh tickets did not start at fence 1")
    state = client.get("/v1/state").json()
    check("no_run_left_working", not state["active_runs"] and not any(r["status"] == "running" for r in runs),
          f"{len(state['active_runs'])} runs still working at the end")
    sizes = (len(state["projects"]), len([n for n in state["nodes"] if n["status"] == "online"]), len(state["accounts"]))
    check("fleet_fully_visible", sizes == (sc.projects, sc.machines, len(sessions)),
          f"state shows {sizes[0]} projects, {sizes[1]} online machines, {sizes[2]} sessions "
          f"(expected {sc.projects}, {sc.machines}, {len(sessions)})")
    picker = client.get(PICKER).json().get("projects", [])
    check("picker_lists_every_project", len(picker) == sc.projects, f"the picker lists {len(picker)} of {sc.projects} projects")
    return out


# --- an isolated run for CI and developers ----------------------------------------------------------------------
@contextmanager
def scratch(admin_url: str, redis_url: str) -> Iterator[tuple[Any, Any]]:
    """A database of its own and a Redis key prefix of its own, removed afterwards (nothing else is touched)."""
    import psycopg
    import redis
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from ..core import testdb
    from ..core.fencing import Leases
    from .db import DB
    name = testdb.new_name()
    admin = psycopg.connect(admin_url, autocommit=True)
    template = os.environ.get("AIFLOW4_TEST_DATABASE_TEMPLATE")  # a database that already carries pgvector
    admin.execute(sql.SQL("CREATE DATABASE {}" + (" TEMPLATE {}" if template else ""))
                  .format(sql.Identifier(name), *([sql.Identifier(template)] if template else [])))
    db = DB(make_conninfo(admin_url, dbname=name))
    r = redis.Redis.from_url(redis_url, decode_responses=True)
    prefix = f"scaleproof{uuid.uuid4().hex[:8]}:"
    try:
        db.migrate()
        yield db, Leases(r, prefix)
    finally:
        db.close()
        for k in r.scan_iter(match=prefix + "*"):
            r.delete(k)
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
