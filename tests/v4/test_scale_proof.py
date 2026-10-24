"""Scale proof (docs/v4/scale-proof.md): 100 projects, 50 machines and 200 simulated AI sessions with realistic ticket
traffic, the performance budgets and their enforcement, the attachment of a run to a release (a regression blocks
it) and the tickets created from findings. Machines and sessions are simulated; no AI provider is called."""
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
def test_scenario_drives_100_projects_50_machines_and_200_sessions(proof):
    report, ctx = proof
    counts = report["counts"]
    assert (counts["projects"], counts["machines"], counts["sessions"]) == (100, 50, 200)
    assert counts["traffic_tickets"] == sum(FULL.wave_sizes) and counts["history_tickets"] == 2000
    assert counts["held_tickets"] > 0 and counts["blocked_tickets"] > 0
    assert counts["pickups"] == counts["traffic_tickets"] - counts["held_tickets"]
    assert report["observed"]["claim_conflicts"] == 0 and report["errors"] == []
    assert ctx.db.one("SELECT count(*) AS n FROM nodes WHERE status='online'")["n"] == 50
    assert ctx.db.one("SELECT count(*) AS n FROM accounts")["n"] == 200
    assert ctx.db.one("SELECT count(DISTINCT project) AS n FROM tickets")["n"] == 100


def test_scenario_behaves_correctly_at_this_size(proof):
    report, _ = proof
    names = {c["name"] for c in report["checks"]}
    assert {"every_ready_ticket_picked_once", "held_tickets_not_picked", "blocked_tickets_wait_for_blocker",
            "claims_on_serving_machines", "one_fence_per_fresh_ticket", "lease_exclusive", "no_run_left_working",
            "fleet_fully_visible", "picker_lists_every_project", "active_runs_visible"} <= names
    assert [c for c in report["checks"] if not c["ok"]] == []


def test_the_plan_is_a_pure_function_of_the_configuration():
    first, second = scale_harness.plan(FULL), scale_harness.plan(FULL)
    assert first["digest"] == second["digest"] and first == second
    assert scale_harness.plan(Scenario(seed=1))["digest"] != first["digest"]
    # every project is served by two machines
    hosts = scale_harness.serving(FULL)
    assert {len(v) for v in hosts.values()} == {2} and len(hosts) == 100
    assert len(scale_harness.sessions_of(FULL)) == 200


def test_two_runs_with_the_same_configuration_are_comparable(proof, tmp_path_factory):
    first, _ = proof
    with scale_harness.scratch(DATABASE_URL, REDIS_URL) as (d, leases):
        second, _ctx = scale_harness.run(d, leases, tmp_path_factory.mktemp("scale-repo"), FULL, release="v4.99.1",
                                         jira=FakeJira())
    assert second["plan_digest"] == first["plan_digest"] and second["scenario"] == first["scenario"]
    assert second["counts"] == first["counts"]
    assert [(c["name"], c["ok"]) for c in second["checks"]] == [(c["name"], c["ok"]) for c in first["checks"]]
    for key in first["measurements"]:
        assert first["measurements"][key]["unit"] == second["measurements"][key]["unit"]
        assert first["measurements"][key]["value"] > 0 and second["measurements"][key]["value"] > 0
    assert [b["key"] for b in second["budgets"]] == [b["key"] for b in first["budgets"]]
    assert second["passed"] is True


# --- AC2: the budgets are written down in one place and enforced ----------------------------------------------
def test_budgets_are_written_down_once_with_their_statistic():
    assert {b.key: (b.limit, b.unit, b.statistic) for b in BUDGETS} == {
        "console_page_load": (2.0, "s", "p95 per page, worst page"),
        "project_picker": (200.0, "ms", "p95"),
        "ticket_pickup": (30.0, "s", "p95")}
    doc = (ROOT / "docs/v4/scale-proof.md").read_text(encoding="utf-8")
    for b in BUDGETS:
        assert b.key in doc and b.statistic in doc and f"{b.limit:g} {b.unit}" in doc


def test_the_100_50_200_run_is_within_every_budget(proof):
    report, _ = proof
    assert [b["message"] for b in report["budgets"] if not b["passed"]] == []
    assert report["passed"] is True and report["failures"] == []
    measured = {b["key"]: b["measured"] for b in report["budgets"]}
    assert measured["console_page_load"] < 2.0 and measured["project_picker"] < 200 and measured["ticket_pickup"] < 30
    # every console page was measured, at the moment most sessions were working
    assert set(report["measurements"]["console_page_load"]["pages"]) == set(scale_harness.PAGES)
    assert report["observed"]["peak_active_runs"] >= 150


def test_percentile_is_nearest_rank():
    assert percentile([], 95) == 0.0
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile(range(1, 101), 95) == 95


def test_a_budget_over_its_limit_fails_the_run_and_the_report_names_budget_and_value(proof):
    report, _ = proof
    slow = json.loads(json.dumps(report))
    slow["measurements"]["console_page_load"]["value"] = 3.2
    verdict = finalize(slow)
    assert verdict["passed"] is False
    [exceeded] = [b for b in verdict["budgets"] if not b["passed"]]
    assert (exceeded["key"], exceeded["measured"], exceeded["limit"], exceeded["unit"]) == ("console_page_load", 3.2, 2.0, "s")
    assert exceeded["message"] == ("Console pages load: console_page_load p95 per page, worst page 3.200 s "
                                   "exceeds the budget of 2 s")
    assert verdict["failures"] == [exceeded["message"]]
    assert [f["signature"] for f in verdict["findings"]] == ["budget:console_page_load"]


def test_a_tightened_budget_fails_the_measured_run(proof):
    report, _ = proof
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


def test_non_200_page_and_picker_responses_fail_the_run(monkeypatch):
    class Client:
        def get(self, _path):
            return SimpleNamespace(json=lambda: {"active_runs": [{}]})

    class Leases:
        def claim(self, *_args):
            return SimpleNamespace(ok=False)

    errors = []
    page_status = {"/cc": 500, scale_harness.PICKER: 503}
    monkeypatch.setattr(scale_harness, "_timed", lambda _client, path: (0.01, page_status.get(path, 200)))
    measured, checks = scale_harness._measure_peak(
        Client(), SimpleNamespace(leases=Leases()), Scenario(page_samples=1, picker_samples=1),
        [(None, {"ticket": {"key": "SCP001-1"}})], errors)
    report = finalize({"measurements": {**measured, "ticket_pickup": {"value": 1.0}},
                       "checks": [{"name": name, "ok": ok, "detail": detail}
                                  for name, (ok, detail) in checks.items()], "errors": errors})
    failed_checks = {check["name"] for check in report["checks"] if not check["ok"]}
    signatures = {finding["signature"] for finding in report["findings"]}
    assert "/cc answered HTTP 500" in errors and "/v1/projects answered HTTP 503" in errors
    assert {"http_response:/cc", "http_response:/v1/projects"} <= failed_checks
    assert {"correctness:http_response:/cc", "correctness:http_response:/v1/projects"} <= signatures
    assert report["passed"] is False


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
    assert scale_proof.blocker(ctx.db, "v4.8.0") == ""  # a release without a run is not judged here


def test_a_regression_blocks_its_release_until_a_later_run_passes(ctx):
    scale_proof.record(ctx, synthetic(console=3.4), "v4.7.0", "ci")
    reason = scale_proof.blocker(ctx.db, "v4.7.0")
    assert reason.startswith("scale proof run #") and "console_page_load" in reason and "3.400 s" in reason
    assert scale_proof.blocker(ctx.db, "v4.6.0") == ""
    scale_proof.record(ctx, synthetic(), "v4.7.0", "ci")
    assert scale_proof.blocker(ctx.db, "v4.7.0") == ""


def test_a_regression_makes_the_jira_version_release_not_ready(ctx):
    from test_version_release import CORE, configure, evidence
    from aiflow.v4.control import version_release
    configure(ctx)
    evidence(ctx, "TESTWP-1")
    version = ctx.jira.version_data["TESTWP"][0]
    project = ctx.repo().projects["TESTWP"]
    assert version_release.evaluate(ctx, project, version)["ready"] is True
    scale_proof.record(ctx, synthetic(pickup=41.0), CORE, "ci")
    blocked = version_release.evaluate(ctx, project, version)
    assert blocked["ready"] is False
    assert any("scale proof run" in r and "ticket_pickup" in r and "41.000 s" in r for r in blocked["reasons"])


def test_release_progress_shows_the_attached_run_and_the_blocking_reason(ctx):
    from aiflow.v4.control import release_progress
    ctx.db.run("INSERT INTO versions (id, project, name, released) VALUES ('900','TESTWP','v4.7.0',false)")
    scale_proof.record(ctx, synthetic(picker=450.0), "v4.7.0", "ci")
    [v] = [v for v in release_progress.project_versions(ctx.db, "TESTWP") if v["version"] == "v4.7.0"]
    assert v["scale_proof"]["passed"] is False and v["ready"] is False
    assert "scale_proof_failed" in [r["cause"] for r in v["reasons"]]
    assert "project_picker" in v["scale_proof"]["blocker"]


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


def test_a_finding_that_returns_after_its_ticket_is_done_gets_a_new_ticket(client, ctx):
    body = {"release": "v4.7.0", "report": synthetic(picker=300.0)}
    first = client.post("/v1/scale-proof/results", json=body, headers=OWNER).json()["tickets"][0]["ticket"]
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key=%s", (first,))
    second = client.post("/v1/scale-proof/results", json={**body, "release": "v4.8.0"}, headers=OWNER).json()["tickets"][0]
    assert second["created"] and second["ticket"] != first and len(ctx.jira.created) == 2


def test_a_jira_outage_keeps_the_finding_and_the_next_run_creates_the_ticket(ctx, monkeypatch):
    run_id = scale_proof.record(ctx, synthetic(pickup=33.0), "v4.7.0", "ci")
    monkeypatch.setattr(ctx.jira, "create_issue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jira down")))
    assert scale_proof.file_tickets(ctx, run_id) == [{"signature": "budget:ticket_pickup", "ticket": "", "created": False}]
    row = ctx.db.one("SELECT ticket, last_error FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert (row["ticket"], row["last_error"]) == ("", "jira down")
    monkeypatch.undo()
    [made] = scale_proof.file_tickets(ctx, run_id)
    assert made["created"] and made["ticket"].startswith("TESTFLOW-")
    assert ctx.db.one("SELECT last_error FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")["last_error"] == ""


def test_a_jira_outage_is_retried_by_a_later_passing_submission(client, ctx, monkeypatch):
    monkeypatch.setattr(ctx.jira, "create_issue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("jira down")))
    failed = client.post("/v1/scale-proof/results", json={"release": "v4.7.0", "report": synthetic(pickup=33.0)},
                         headers=OWNER).json()
    assert failed["passed"] is False
    assert failed["tickets"] == [{"signature": "budget:ticket_pickup", "ticket": "", "created": False}]
    pending = ctx.db.one("SELECT ticket, last_error FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")
    assert (pending["ticket"], pending["last_error"]) == ("", "jira down")

    monkeypatch.undo()
    passed = client.post("/v1/scale-proof/results", json={"release": "v4.7.1", "report": synthetic()},
                         headers=OWNER).json()
    assert passed["passed"] is True
    assert passed["tickets"] == [{"signature": "budget:ticket_pickup", "ticket": "TESTFLOW-900", "created": True}]
    assert len(ctx.jira.created) == 1
    assert ctx.db.one("SELECT last_error FROM scale_proof_findings WHERE signature='budget:ticket_pickup'")["last_error"] == ""


def test_the_real_run_has_nothing_to_report(proof, ctx):
    report, _ = proof
    run_id = scale_proof.record(ctx, report, "v4.99.0", "test")
    assert scale_proof.file_tickets(ctx, run_id) == [] and not hasattr(ctx.jira, "created")


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
    assert "scaleProofPanel(d.scale_proof)" in page and "scale_proof_failed" in page


# --- CI command --------------------------------------------------------------------------------------------------
@contextmanager
def _no_scratch(*_args):
    yield object(), object()


def test_cli_exits_non_zero_on_a_regression_and_writes_the_report(tmp_path, monkeypatch, capsys):
    import httpx
    from aiflow.v4 import cli

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"run_id": 7, "tickets": []}

    monkeypatch.setattr(scale_harness, "scratch", _no_scratch)
    monkeypatch.setattr(scale_harness, "run", lambda *a, release="", **k: (finalize({**synthetic(console=4.0), "release": release}), None))
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: Response())
    monkeypatch.setenv("AIFLOW4_OWNER_TOKEN", "owner-secret-value")
    out = tmp_path / "scale.json"
    assert cli.main(["scale-proof", "--release", "v4.9.0", "--out", str(out), "--submit", "https://control.example",
                     "--database-url", "postgresql://x", "--redis-url", "redis://x"]) == 1
    saved = json.loads(out.read_text())
    assert saved["release"] == "v4.9.0" and saved["passed"] is False
    assert "EXCEEDED Console pages load: console_page_load" in capsys.readouterr().out


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
    assert cli.main(["scale-proof", "--release", "v4.9.1", "--submit", "https://control.example",
                     "--database-url", "", "--redis-url", ""]) == 2


def test_cli_requires_submission_url_and_owner_token(monkeypatch, capsys):
    from aiflow.v4 import cli
    monkeypatch.delenv("AIFLOW4_OWNER_TOKEN", raising=False)
    args = ["scale-proof", "--release", "v4.9.2", "--database-url", "postgresql://x", "--redis-url", "redis://x"]
    assert cli.main(args) == 3
    assert "requires --submit" in capsys.readouterr().err
    assert cli.main([*args, "--submit", "https://control.example"]) == 3
    assert "requires AIFLOW4_OWNER_TOKEN" in capsys.readouterr().err


def test_cli_fails_when_submission_cannot_complete_even_after_a_budget_failure(monkeypatch, capsys):
    import httpx
    from aiflow.v4 import cli

    class Response:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("unavailable", request=None, response=None)

    monkeypatch.setattr(scale_harness, "scratch", _no_scratch)
    monkeypatch.setattr(scale_harness, "run", lambda *a, **k: (finalize(synthetic(console=4.0)), None))
    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: Response())
    monkeypatch.setenv("AIFLOW4_OWNER_TOKEN", "owner-secret-value")
    result = cli.main(["scale-proof", "--release", "v4.9.3", "--submit", "https://control.example",
                       "--database-url", "postgresql://x", "--redis-url", "redis://x"])
    assert result == 3
    assert "could not attach" in capsys.readouterr().err


def test_the_release_workflow_runs_the_scale_proof_before_publishing():
    workflow = (ROOT / ".github/workflows/release-v4.yml").read_text(encoding="utf-8")
    assert "aiflow4 scale-proof --release" in workflow
    assert workflow.index("aiflow4 scale-proof") < workflow.index("Build and push control-api image")
    assert "scale-proof-$VERSION.json" in workflow
    assert "AIFLOW4_CONTROL_URL: ${{ secrets.AIFLOW4_CONTROL_URL }}" in workflow
    assert "AIFLOW4_OWNER_TOKEN: ${{ secrets.AIFLOW4_OWNER_TOKEN }}" in workflow
    assert '--submit "$AIFLOW4_CONTROL_URL"' in workflow
    assert 'if [ -n "$AIFLOW4_CONTROL_URL" ]' not in workflow
