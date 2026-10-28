"""Scale proof (docs/v4/scale-proof.md): 100 projects, 50 machines and 200 simulated AI sessions with realistic ticket
traffic, the performance budgets and their enforcement, the release proof gate and the tickets created from findings.
Machines and sessions are simulated; no AI provider is called."""
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from conftest import DATABASE_URL, REDIS_URL, FakeJira
from aiflow.v4.control import scale_harness, scale_proof
from aiflow.v4.control.scale_harness import Scenario
from aiflow.v4.control.scale_proof import BUDGETS, Budget, evaluate, finalize, percentile

ROOT = Path(__file__).resolve().parents[2]
FULL = Scenario()
OWNER = {"Authorization": "Bearer owner-secret"}


@pytest.fixture(scope="module")
def proof(tmp_path_factory):
    """One complete run of the 100/50/200 scenario in a database and Redis prefix of its own (shared by the tests)."""
    with scale_harness.scratch(DATABASE_URL, REDIS_URL) as (d, leases):
        yield scale_harness.run(d, leases, tmp_path_factory.mktemp("scale-repo"), FULL, release="v4.99.0",
                                jira=FakeJira())


# --- AC1: the scenario ---------------------------------------------------------------------------------------
def test_scratch_schema_keeps_pgvector_in_public_for_following_tests():
    with scale_harness.scratch(DATABASE_URL, REDIS_URL) as (db, _leases):
        extension = db.one("SELECT n.nspname AS schema FROM pg_extension e "
                           "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")
        assert extension == {"schema": "public"}


def test_scenario_drives_100_projects_50_machines_and_200_sessions(proof):
    report, ctx = proof
    counts = report["counts"]
    assert (counts["projects"], counts["machines"], counts["sessions"]) == (100, 50, 200)
    assert counts["accounts"] == 100
    assert counts["traffic_tickets"] == sum(FULL.wave_sizes) and counts["history_tickets"] == 2000
    assert counts["held_tickets"] > 0 and counts["blocked_tickets"] > 0
    assert counts["pickups"] == counts["traffic_tickets"] - counts["held_tickets"]
    assert isinstance(report["observed"]["claim_conflicts"], int)
    assert report["errors"] == [], "\n".join(report["errors"])
    assert ctx.db.one("SELECT count(*) AS n FROM nodes WHERE status='online'")["n"] == 50
    assert ctx.db.one("SELECT count(*) AS n FROM accounts")["n"] == 100
    assert report["environment"]["api_execution"] == "FastAPI TestClient in-process"
    assert report["environment"]["persistence"] == "PostgreSQL and Redis"
    assert report["environment"]["local_embeddings_enabled"] is False
    assert report["environment"]["max_parallel_requests"] == 36
    assert ctx.db.one("SELECT count(DISTINCT project) AS n FROM tickets")["n"] == 100


def test_scenario_behaves_correctly_at_this_size(proof):
    report, _ = proof
    names = {c["name"] for c in report["checks"]}
    assert {"every_ready_ticket_picked_once", "held_tickets_not_picked", "blocked_tickets_wait_for_blocker",
            "claims_on_serving_machines", "one_fence_per_fresh_ticket", "lease_exclusive", "no_run_left_working",
            "fleet_fully_visible", "picker_lists_every_project", "active_runs_visible", "measurement_overlap",
            "measurement_active_threshold"} <= names
    assert [c for c in report["checks"] if not c["ok"]] == []


def test_the_plan_is_a_pure_function_of_the_configuration():
    first, second = scale_harness.plan(FULL), scale_harness.plan(FULL)
    assert first["digest"] == second["digest"] and first == second
    assert scale_harness.plan(Scenario(seed=1))["digest"] != first["digest"]
    # every project is served by two machines
    hosts = scale_harness.serving(FULL)
    assert {len(v) for v in hosts.values()} == {2} and len(hosts) == 100
    sessions = scale_harness.sessions_of(FULL)
    assert len(sessions) == 200 and len({s["id"] for s in sessions}) == 100
    assert set(__import__("collections").Counter(s["id"] for s in sessions).values()) == {2}
    assert scale_harness.plan(FULL)["scenario"]["claim_concurrency"] == 24


def test_active_simulated_runs_are_renewed_by_the_worker_heartbeat_loop():
    from threading import Event

    called = Event()
    worker_requests = []
    heartbeat_requests = []

    class Response:
        status_code = 200

    class Client:
        def __init__(self, requests):
            self.requests = requests

        def post(self, path, **kwargs):
            self.requests.append((path, kwargs))
            called.set()
            return Response()

    headers = {"scale-node-01": {"Authorization": "Bearer node-token"}}
    keeper = scale_harness._RunLeaseKeeper(Client(worker_requests), headers, [], max_parallel=1,
                                           heartbeat_client=Client(heartbeat_requests), interval_s=0.01)
    keeper.add({"node": "scale-node-01"}, {"run_id": "run-1", "fence": 7})
    try:
        assert called.wait(timeout=1)
    finally:
        keeper.close()

    assert worker_requests == []
    [(path, kwargs)] = heartbeat_requests
    assert path == "/v1/runs/run-1/heartbeat"
    assert kwargs["json"]["fence"] == 7
    assert kwargs["headers"] == headers["scale-node-01"]


def test_completing_a_simulated_run_drains_its_inflight_heartbeat():
    from threading import Event, Thread

    heartbeat_started = Event()
    allow_heartbeat_to_finish = Event()
    completion_started = Event()
    completion_finished = Event()
    requests = []

    class Response:
        status_code = 200

    class Client:
        def post(self, path, **kwargs):
            requests.append((path, kwargs))
            heartbeat_started.set()
            assert allow_heartbeat_to_finish.wait(timeout=2)
            return Response()

    headers = {"scale-node-01": {"Authorization": "Bearer node-token"}}
    keeper = scale_harness._RunLeaseKeeper(Client(), headers, [], max_parallel=1, interval_s=0.01)
    keeper.add({"node": "scale-node-01"}, {"run_id": "run-1", "fence": 7})
    assert heartbeat_started.wait(timeout=1)

    def complete():
        completion_started.set()
        keeper.complete("run-1")
        completion_finished.set()

    thread = Thread(target=complete)
    thread.start()
    try:
        assert completion_started.wait(timeout=1)
        assert not completion_finished.wait(timeout=0.02)
    finally:
        allow_heartbeat_to_finish.set()
        assert completion_finished.wait(timeout=1)
        keeper.close()
        thread.join(timeout=1)

    assert len(requests) == 1


def test_console_view_endpoints_work_with_a_small_local_fleet(tmp_path):
    scenario = Scenario(projects=10, machines=4, sessions=8, wave_sizes=(12,), history_per_project=2,
                        page_samples=1, picker_samples=1, claim_concurrency=4, measurement_concurrency=4,
                        measurement_min_active=5)
    with scale_harness.scratch(DATABASE_URL, REDIS_URL) as (db, leases):
        report, _ctx = scale_harness.run(db, leases, tmp_path / "scale-repo", scenario, release="v4.99.0",
                                         jira=FakeJira())
    assert report["errors"] == [], "\n".join(report["errors"])
    assert set(report["measurements"]["console_page_load"]["views"]) == set(scale_harness.PAGES)


def test_every_top_level_console_tab_has_a_scale_measurement():
    import re

    html = (ROOT / "aiflow/v4/control/ui/console.html").read_text(encoding="utf-8")
    tabs = set(re.findall(r'<button class="tab"[^>]*data-view="([^"]+)"', html))
    measured = {path.lstrip("/") for path in scale_harness.PAGES}
    assert tabs == measured
    assert len(tabs) == 18


def test_full_run_report_is_bound_to_the_declared_plan(proof):
    report, _ = proof
    assert report["scenario"] == scale_harness.plan(FULL)["scenario"]
    assert report["plan_digest"] == scale_harness.plan(FULL)["digest"]


# --- AC2: the budgets are written down in one place and enforced ----------------------------------------------
def test_budgets_are_written_down_once_with_their_statistic():
    assert {b.key: (b.limit, b.unit, b.statistic) for b in BUDGETS} == {
        "console_page_load": (2.0, "s", "p95 per view, worst view"),
        "project_picker": (200.0, "ms", "p95"),
        "ticket_pickup": (30.0, "s", "p95")}
    doc = (ROOT / "docs/v4/scale-proof.md").read_text(encoding="utf-8")
    for b in BUDGETS:
        assert b.key in doc and b.statistic in doc and f"{b.limit:g} {b.unit}" in doc


def test_the_100_50_200_run_is_within_every_budget(proof):
    report, _ = proof
    failures = [b["message"] for b in report["budgets"] if not b["passed"]]
    assert failures == [], json.dumps({"failures": failures,
                                       "console_page_load": report["measurements"].get("console_page_load"),
                                       "project_picker": report["measurements"].get("project_picker"),
                                       "ticket_pickup": report["measurements"].get("ticket_pickup"),
                                       "counts": report["counts"], "observed": report["observed"]}, indent=2)
    assert report["passed"] is True and report["failures"] == []
    measured = {b["key"]: b["measured"] for b in report["budgets"]}
    assert measured["console_page_load"] < 2.0 and measured["project_picker"] < 200 and measured["ticket_pickup"] < 30
    # every console view was measured while real worker claims were in flight
    assert set(report["measurements"]["console_page_load"]["views"]) == set(scale_harness.PAGES)
    assert report["observed"]["peak_active_runs"] >= 150


def test_percentile_is_nearest_rank():
    assert percentile([], 95) == 0.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile(range(1, 101), 95) == 95


def test_a_budget_over_its_limit_fails_the_run_and_the_report_names_budget_and_value():
    slow = synthetic()
    slow["measurements"]["console_page_load"]["value"] = 3.2
    verdict = finalize(slow)
    assert verdict["passed"] is False
    [exceeded] = [b for b in verdict["budgets"] if not b["passed"]]
    assert (exceeded["key"], exceeded["measured"], exceeded["limit"], exceeded["unit"]) == ("console_page_load", 3.2, 2.0, "s")
    assert exceeded["message"] == ("Console views load: console_page_load p95 per view, worst view 3.200 s "
                                   "exceeds the budget of 2 s")
    assert verdict["failures"] == [exceeded["message"]]
    assert [f["signature"] for f in verdict["findings"]] == ["budget:console_page_load"]


def test_a_tightened_budget_fails_the_measured_run():
    report = synthetic()
    tight = (Budget("project_picker", "Project picker answers", 0.001, "ms", "p95", ""),)
    verdict = finalize(report, tight)
    assert verdict["passed"] is False
    assert "project_picker p95 " in verdict["failures"][0] and "exceeds the budget of 0.001 ms" in verdict["failures"][0]


def test_an_unmeasured_budget_and_a_failed_check_both_fail_the_run():
    assert [b["passed"] for b in evaluate({"project_picker": 5.0})] == [False, True, False]
    broken = finalize({"measurements": {"console_page_load": {"value": 0.5}, "project_picker": {"value": 5},
                                        "ticket_pickup": {"value": 1}},
                       "checks": [{"name": "every_ready_ticket_picked_once", "ok": False, "detail": "3 not picked"}]})
    assert broken["passed"] is False
    assert broken["failures"] == ["correctness: every_ready_ticket_picked_once: 3 not picked"]


# --- AC3: the result is attached to a release; a regression blocks it ------------------------------------------
def synthetic(console=0.5, picker=10.0, pickup=4.0, ok=True):
    return {"scenario": {"projects": 100, "machines": 50, "sessions": 200, "seed": 236}, "plan_digest": "d" * 64,
            "counts": {"projects": 100}, "duration_s": 70.0,
            "measurements": {"console_page_load": {"value": console}, "project_picker": {"value": picker},
                             "ticket_pickup": {"value": pickup}},
            "checks": [{"name": "lease_exclusive", "ok": ok, "detail": "a second claim was " + ("refused" if ok else "granted")}]}


def test_a_passing_run_is_attached_to_its_release_and_does_not_block(ctx):
    run_id = scale_proof.record(ctx, synthetic(), "v4.7.0", "ci")
    row = ctx.db.one("SELECT release, passed, source, report FROM scale_proof_runs WHERE id=%s", (run_id,))
    assert (row["release"], row["passed"], row["source"]) == ("v4.7.0", True, "ci")
    assert row["report"]["budgets"][0]["key"] == "console_page_load"
    assert scale_proof.blocker(ctx.db, "v4.7.0") == ""
    assert scale_proof.blocker(ctx.db, "v4.8.0").startswith("scale proof is missing")


def test_a_regression_blocks_its_release_until_a_later_run_passes(ctx):
    scale_proof.record(ctx, synthetic(console=3.4), "v4.7.0", "ci")
    reason = scale_proof.blocker(ctx.db, "v4.7.0")
    assert reason.startswith("scale proof run #") and "console_page_load" in reason and "3.400 s" in reason
    assert scale_proof.blocker(ctx.db, "v4.6.0").startswith("scale proof is missing")
    scale_proof.record(ctx, synthetic(), "v4.7.0", "ci")
    assert scale_proof.blocker(ctx.db, "v4.7.0") == ""


def test_a_regression_makes_the_jira_version_release_not_ready(ctx):
    from test_version_release import CORE, configure, evidence
    from aiflow.v4.control import version_release
    configure(ctx, with_scale_proof=False)
    evidence(ctx, "TESTWP-1")
    version = ctx.jira.version_data["TESTWP"][0]
    project = ctx.repo().projects["TESTWP"]
    missing = version_release.evaluate(ctx, project, version)
    assert missing["ready"] is False and any("scale proof is missing" in r for r in missing["reasons"])
    scale_proof.record(ctx, synthetic(), CORE, "ci")
    assert version_release.evaluate(ctx, project, version)["ready"] is True
    scale_proof.record(ctx, synthetic(pickup=41.0), CORE, "ci")
    blocked = version_release.evaluate(ctx, project, version)
    assert blocked["ready"] is False
    assert any("scale proof run" in r and "ticket_pickup" in r and "41.000 s" in r for r in blocked["reasons"])


def test_already_released_versions_are_grandfathered_without_a_proof(ctx):
    from test_version_release import CORE, configure, evidence
    from aiflow.v4.control import version_release

    configure(ctx)
    evidence(ctx, "TESTWP-1")
    version = {**ctx.jira.version_data["TESTWP"][0], "released": True}
    assert version_release.evaluate(ctx, ctx.repo().projects["TESTWP"], version)["ready"] is True


def test_release_progress_shows_the_attached_run_and_the_blocking_reason(ctx):
    from aiflow.v4.control import release_progress
    ctx.db.run("INSERT INTO versions (id, project, name, released) VALUES ('900','TESTWP','v4.7.0',false)")
    scale_proof.record(ctx, synthetic(picker=450.0), "v4.7.0", "ci")
    [v] = [v for v in release_progress.project_versions(ctx.db, "TESTWP") if v["version"] == "v4.7.0"]
    assert v["scale_proof"]["passed"] is False and v["ready"] is False
    assert "scale_proof_failed" in [r["cause"] for r in v["reasons"]]
    assert "project_picker" in v["scale_proof"]["blocker"]


def test_release_progress_shows_when_an_unreleased_version_has_no_proof(ctx):
    from aiflow.v4.control import release_progress

    ctx.db.run("INSERT INTO versions (id, project, name, released) VALUES ('901','TESTWP','v4.8.0',false)")
    [version] = [v for v in release_progress.project_versions(ctx.db, "TESTWP") if v["version"] == "v4.8.0"]
    assert version["scale_proof"] is None and version["ready"] is False
    assert "scale_proof_missing" in [reason["cause"] for reason in version["reasons"]]


def test_ci_submits_a_run_to_the_control_plane_which_judges_it_itself(client, ctx):
    assert client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": synthetic()}).status_code == 401
    assert client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": {"x": 1}},
                       headers=OWNER).status_code == 422
    # a report that claims to have passed cannot hide a measurement over its budget
    lying = {**synthetic(console=5.0), "passed": True, "failures": [], "budgets": []}
    r = client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": lying}, headers=OWNER).json()
    assert r["passed"] is False and r["blocked"] is True and "console_page_load" in r["failures"][0]
    ok = client.post("/v1/scale-proof/results", json={"release": "v4.7.1", "report": synthetic()}, headers=OWNER).json()
    assert ok["passed"] is True and ok["blocked"] is False and ok["tickets"] == []
    listed = client.get("/v1/scale-proof").json()
    assert [(x["release"], x["passed"]) for x in listed["runs"]] == [("v4.7.1", True), ("v4.7.0", False)]
    assert [b["key"] for b in listed["budgets"]] == [b.key for b in BUDGETS]
    assert client.get("/v1/state").json()["scale_proof"]["runs"][0]["release"] == "v4.7.1"


# --- AC4: findings become tickets automatically -----------------------------------------------------------------
def test_each_finding_becomes_one_bug_ticket_without_a_manual_step(client, ctx):
    r = client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": synthetic(console=3.2, ok=False)},
                    headers=OWNER).json()
    assert {t["signature"] for t in r["tickets"]} == {"budget:console_page_load", "correctness:lease_exclusive"}
    assert all(t["created"] and t["ticket"] for t in r["tickets"])
    created = ctx.jira.created
    assert len(created) == 2 and {c["issuetype"] for c in created} == {"Bug"} and {c["project"] for c in created} == {"TESTFLOW"}
    budget = next(c for c in created if "console_page_load" in c["summary"])
    assert "3.200 s" in budget["summary"] and "2 s" in budget["summary"]
    assert "flow-bug" in budget["labels"] and "scale-proof" in budget["labels"]
    assert "v4.7.0" in budget["description"] and "100 projects, 50 machines, 200 simulated AI sessions" in budget["description"]
    assert ctx.db.one("SELECT count(*) AS n FROM tickets WHERE key = ANY(%s)", ([t["ticket"] for t in r["tickets"]],))["n"] == 2
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='scale_proof_create'")["n"] == 2


def test_a_repeated_finding_does_not_create_a_second_ticket(client, ctx):
    body = {"release": "v4.7.0", "report": synthetic(picker=300.0)}
    first = client.post("/v1/scale-proof/results", json=body, headers=OWNER).json()
    again = client.post("/v1/scale-proof/results", json={**body, "release": "v4.7.1"}, headers=OWNER).json()
    assert again["tickets"] == [{"signature": "budget:project_picker", "ticket": first["tickets"][0]["ticket"], "created": False}]
    assert len(ctx.jira.created) == 1
    assert ctx.db.one("SELECT occurrences FROM scale_proof_findings WHERE signature='budget:project_picker'")["occurrences"] == 2
    assert ctx.jira.comments and "project_picker" in ctx.jira.comments[-1][1]


def test_concurrent_filing_creates_only_one_ticket_for_a_finding(ctx, monkeypatch):
    run_id = scale_proof.record(ctx, synthetic(pickup=33.0), "v4.7.0", "ci")
    create_issue = ctx.jira.create_issue
    calls = 0
    calls_lock = threading.Lock()
    start = threading.Barrier(2)

    def slow_create(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.2)
        return create_issue(*args, **kwargs)

    def submit():
        start.wait(timeout=5)
        return scale_proof.file_tickets(ctx, run_id)

    monkeypatch.setattr(ctx.jira, "create_issue", slow_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: submit(), range(2)))

    created = [ticket for result in results for ticket in result if ticket["created"]]
    assert calls == 1 and len(ctx.jira.created) == 1
    assert len(created) == 1
    ticket = ctx.db.one("SELECT ticket FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert ticket["ticket"] == created[0]["ticket"]


def test_out_of_order_filing_keeps_the_latest_run_evidence(ctx):
    older = scale_proof.record(ctx, synthetic(pickup=33.0), "v4.7.0", "ci")
    newer = scale_proof.record(ctx, synthetic(pickup=35.0), "v4.7.1", "ci")

    [created] = scale_proof.file_tickets(ctx, newer)
    [late] = scale_proof.file_tickets(ctx, older)

    assert created["created"] and late["ticket"] == created["ticket"] and not late["created"]
    finding = ctx.db.one("SELECT last_run_id,occurrences FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert finding == {"last_run_id": newer, "occurrences": 2}
    assert f"run #{newer} of release v4.7.1" in ctx.jira.created[0]["description"]


def test_a_finding_that_returns_after_its_ticket_is_done_gets_a_new_ticket(client, ctx):
    body = {"release": "v4.7.0", "report": synthetic(picker=300.0)}
    first = client.post("/v1/scale-proof/results", json=body, headers=OWNER).json()["tickets"][0]["ticket"]
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key=%s", (first,))
    second = client.post("/v1/scale-proof/results", json={**body, "release": "v4.8.0"}, headers=OWNER).json()["tickets"][0]
    assert second["created"] and second["ticket"] != first and len(ctx.jira.created) == 2


def test_a_jira_outage_retries_the_persisted_finding_on_a_later_passing_submission(client, ctx, monkeypatch):
    create_issue = ctx.jira.create_issue
    monkeypatch.setattr(ctx.jira, "create_issue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jira down")))
    failed = client.post("/v1/scale-proof/results",
                         json={"release": "v4.7.0", "report": synthetic(pickup=33.0)}, headers=OWNER).json()
    assert failed["passed"] is False
    assert failed["tickets"] == [{"signature": "budget:ticket_pickup", "ticket": "", "created": False}]
    pending = ctx.db.one("SELECT ticket,last_error,first_run_id,last_run_id,occurrences"
                         " FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert (pending["ticket"], pending["last_error"], pending["first_run_id"], pending["last_run_id"],
            pending["occurrences"]) == ("", "jira down", failed["run_id"], failed["run_id"], 1)

    monkeypatch.setattr(ctx.jira, "create_issue", create_issue)
    passed = client.post("/v1/scale-proof/results",
                         json={"release": "v4.7.1", "report": synthetic()}, headers=OWNER).json()
    assert passed["passed"] is True
    [made] = passed["tickets"]
    assert made["signature"] == "budget:ticket_pickup" and made["created"]
    assert made["ticket"].startswith("TESTFLOW-") and len(ctx.jira.created) == 1
    pending = ctx.db.one("SELECT ticket,last_error,first_run_id,last_run_id,occurrences"
                         " FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert (pending["ticket"], pending["last_error"], pending["first_run_id"], pending["last_run_id"],
            pending["occurrences"]) == (made["ticket"], "", failed["run_id"], failed["run_id"], 1)
    description = ctx.jira.created[0]["description"]
    assert f"run #{failed['run_id']} of release v4.7.0" in description

    assert scale_proof.file_tickets(ctx, passed["run_id"]) == []
    assert len(ctx.jira.created) == 1


def test_the_real_run_has_nothing_to_report(proof, ctx):
    report, _ = proof
    run_id = scale_proof.record(ctx, report, "v4.99.0", "test")
    tickets = scale_proof.file_tickets(ctx, run_id)
    if report["passed"]:
        assert tickets == [] and not hasattr(ctx.jira, "created")
    else:
        assert {ticket["signature"] for ticket in tickets} == {finding["signature"] for finding in report["findings"]}
        assert all(ticket["created"] for ticket in tickets)


# --- the console shows the attachment and the tickets ------------------------------------------------------------
def test_command_center_lists_runs_budgets_and_tickets(client, ctx):
    from test_console import run_script
    from test_business import DOM
    client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": synthetic(console=3.2)}, headers=OWNER)
    client.post("/v1/scale-proof/results", json={"release": "v4.7.1", "report": synthetic()}, headers=OWNER)
    summary = client.get("/v1/state").json()["scale_proof"]
    html = run_script(DOM + f"console.log(scaleProofPanel({json.dumps(summary)}));")
    assert 'data-passed="no"' in html and 'data-passed="yes"' in html
    assert "v4.7.0" in html and "v4.7.1" in html and "Regression" in html and "Within budget" in html
    assert 'data-budget="console_page_load" data-state="exceeded"' in html and "3.2 s" in html and "2 s" in html
    assert 'data-ticket="TESTFLOW-900"' in html
    empty = run_script(DOM + "console.log(scaleProofPanel({runs: [], budgets: [], findings: []}));")
    assert "No scale proof run is attached" in empty
    page = (ROOT / "aiflow/v4/control/ui/console.html").read_text(encoding="utf-8")
    readiness = (ROOT / "aiflow/v4/control/ui/release-readiness.js").read_text(encoding="utf-8")
    assert "scaleProofPanel(d.scale_proof," in page and "release-readiness.js" in page and "scale_proof_failed" in readiness


# --- CI command --------------------------------------------------------------------------------------------------
@contextmanager
def _no_scratch(*_args):
    yield object(), object()


def test_cli_exits_non_zero_on_a_regression_and_writes_the_report(tmp_path, monkeypatch, capsys):
    from aiflow.v4 import cli
    monkeypatch.setattr(scale_harness, "scratch", _no_scratch)
    monkeypatch.setattr(scale_harness, "run", lambda *a, release="", **k: (finalize({**synthetic(console=4.0), "release": release}), None))
    out = tmp_path / "scale.json"
    assert cli.main(["scale-proof", "--release", "v4.9.0", "--out", str(out), "--database-url", "postgresql://x",
                     "--redis-url", "redis://x"]) == 1
    saved = json.loads(out.read_text())
    assert saved["release"] == "v4.9.0" and saved["passed"] is False
    assert "EXCEEDED Console views load: console_page_load" in capsys.readouterr().out


def test_cli_attaches_the_result_to_the_control_plane_and_a_pass_exits_zero(monkeypatch, capsys):
    import httpx
    from aiflow.v4 import cli
    posted = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"run_id": 7, "tickets": []}

    monkeypatch.setattr(scale_harness, "scratch", _no_scratch)
    monkeypatch.setattr(scale_harness, "run", lambda *a, release="", **k: (finalize(synthetic()), None))
    monkeypatch.setattr(httpx, "post", lambda url, **k: posted.append((url, k)) or Response())
    monkeypatch.setenv("AIFLOW4_OWNER_TOKEN", "owner-secret-value")
    assert cli.main(["scale-proof", "--release", "v4.9.1", "--submit", "https://control.example/", "--database-url",
                     "postgresql://x", "--redis-url", "redis://x"]) == 0
    [(url, kwargs)] = posted
    assert url == "https://control.example/v1/scale-proof/results"
    assert kwargs["json"]["release"] == "v4.9.1" and kwargs["headers"]["Authorization"] == "Bearer owner-secret-value"
    assert "owner-secret-value" not in capsys.readouterr().out
    monkeypatch.delenv("AIFLOW4_TEST_DATABASE_URL", raising=False)
    assert cli.main(["scale-proof", "--release", "v4.9.1", "--database-url", "", "--redis-url", ""]) == 2


def test_the_release_workflow_runs_the_scale_proof_before_publishing():
    workflow = (ROOT / ".github/workflows/release-v4.yml").read_text(encoding="utf-8")
    assert "aiflow4 scale-proof --release" in workflow
    assert "--submit \"$AIFLOW4_CONTROL_URL\"" in workflow
    assert "AIFLOW4_CONTROL_URL is required" in workflow and "AIFLOW4_OWNER_TOKEN is required" in workflow
    assert workflow.index("aiflow4 scale-proof") < workflow.index("Build and push control-api image")
    assert "scale-proof-$VERSION.json" in workflow


def test_scale_proof_docs_name_the_release_api_routes_and_submission_arguments():
    doc = (ROOT / "docs/v4/scale-proof.md").read_text(encoding="utf-8")
    for route in ("GET /v1/deploy/state", "POST /v1/version-releases/claim",
                  "GET /v1/scale-proof", "POST /v1/scale-proof/results"):
        assert route in doc
    assert "--submit \"$AIFLOW4_CONTROL_URL\"" in doc and "AIFLOW4_OWNER_TOKEN" in doc
