"""A tool missing on a machine is a machine problem (docs/v4/missing-tools.md): AC1-AC6 of JAIF-242."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import add_ticket, join_node
from aiflow.v4.control import tooling
from aiflow.v4.core.toolchains import (CATALOG_TOOLS, known_requirements, parse_command_not_found_requirements,
                                       parse_missing_requirements)
from aiflow.v4.node import agent, operations, probes
from aiflow.v4.worker import dev
from aiflow.v4.worker.dev import Claimed, build_prompt, machine_block

CC_TOOLS = {"git": "git version 2.43", "uv": "uv 0.5", "claude": "2.1.305", "php": "missing"}
NODE1_TOOLS = {"git": "git version 2.43", "uv": "uv 0.5", "codex": "1.0", "php": "PHP 8.3.1", "composer": "2.8"}


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def heartbeat(client, name, headers, tools):
    return client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2, "tools": tools}, headers=headers).json()


def claim_and_report_missing(client, headers, ticket, account="claude-01", tools=("php",),
                             summary="phpunit cannot run"):
    r = client.post("/v1/work/claim", json={"ticket": ticket, "account": account}, headers=headers).json()
    assert r["claimed"], r
    done = client.post(f"/v1/runs/{r['run_id']}/result", json={
        "fence": r["fence"], "outcome": "tool_missing", "summary": summary,
        "details": {"missing_tools": list(tools)}}, headers=headers)
    assert done.status_code == 200
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=headers)
    return r


# --- AC1: the missing requirement and the machine are recorded, the owner is not asked -------------------------
def test_marker_and_known_names_are_read_from_the_blocked_reason():
    assert parse_missing_requirements("MISSING-TOOL: php, composer - phpunit cannot run") == ["php", "composer"]
    assert parse_missing_requirements("MISSING-SERVICE: postgres - tests need a database") == ["postgres"]
    assert parse_missing_requirements("composer is not installed on this machine") == []
    assert parse_missing_requirements("bash: php: command not found") == []
    assert parse_missing_requirements("bash: line 1: docker: command not found") == []
    assert parse_command_not_found_requirements([
        {"command": "php -v", "output": "bash: php: command not found", "exit_code": 127},
        {"command": "bash -lc 'docker version'", "output": "bash: line 1: docker: command not found", "failed": True},
    ]) == ["php", "docker"]
    assert parse_command_not_found_requirements([
        {"command": "echo 'bash: php: command not found'", "output": "bash: php: command not found", "exit_code": 1},
        {"command": "php -v", "output": "bash: php: command not found", "exit_code": 0},
    ]) == []
    assert parse_command_not_found_requirements([
        {"command": "echo 'bash: php: command not found'; php --bad 2>/dev/null",
         "output": "bash: php: command not found", "exit_code": 1},
    ]) == []
    assert parse_missing_requirements("MISSING-TOOL: phpunit - not a catalogue tool") == []
    assert parse_missing_requirements("The summary mentions docker, php, and a missing test fixture") == []
    assert parse_missing_requirements("AC2 contradicts AC4; which one wins?") == []


class _API:
    node_name = "cc"

    def __init__(self, raise_result=None):
        self.calls = []
        self.raise_result = raise_result

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path == "/v1/escalations/raise":
            if self.raise_result is not None:
                return self.raise_result
            missing = "MISSING-TOOL" in body["question"]
            return {"class": "missing_tool_or_environment" if missing else "new_business_decision",
                    "label": "Missing tool or environment" if missing else "New business decision",
                    "reason": "reported missing tool" if missing else "requirements conflict",
                    "resolved": missing, "outcome": "tool_missing" if missing else "blocked",
                    "answer": "", "citation": "", "missing_tools": []}
        return {"token": "github-token", "url": "https://github.com/x/y/pull/1", "number": 1}


def _claimed():
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop", "checks": ["vendor/bin/phpunit"],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash"}}
    return Claimed(run_id="run-1", fence=3, run_token="t", account="claude-01", provider="claude", model="opus",
                   effort="", ticket={"key": "TESTWP-1", "project": "TESTWP", "summary": "S", "description": "AC1",
                                      "labels": [], "priority": "Medium", "type": "Story"},
                   project=project, identity=("bot", "bot@example.test"))


def _run_dev(monkeypatch, tmp_path, verdict, reason, raise_result=None, final_text="", command_results=None):
    class Git:
        def __init__(self, *_a, **_k):
            pass

        def mirror(self, *_a):
            return "mirror"

        def foreign_worktree(self, *_a):
            return None

        def worktree(self, _m, _p, _b, base):
            return "origin/" + base

        def changed(self, _path):
            return []

    class Adapter:
        argv = lambda self, _spec: []

        def run(self, *_a, **_k):
            return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, quota={}, error_kind="", reset_at=None,
                                   verdict=verdict, verdict_reason=reason, error_message="", final_text=final_text,
                                   permission_denials=[], exit_code=0, duration_s=1, log_path="",
                                   command_results=command_results or [])

    monkeypatch.setattr(dev, "Git", Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _p: Adapter())
    api = _API(raise_result)
    return dev.run_dev(api, _claimed(), str(tmp_path), "https://control", 3600, []), api


def test_missing_tool_is_not_a_question_for_the_owner(monkeypatch, tmp_path):
    result, api = _run_dev(monkeypatch, tmp_path, "BLOCKED", "MISSING-TOOL: php, composer - phpunit cannot run")
    assert result["outcome"] == "tool_missing" and result["details"]["missing_tools"] == ["php", "composer"]
    assert result["details"]["tool_missing_evidence"] == "explicit_marker"
    assert result["details"]["escalation_class"] == "missing_tool_or_environment"
    assert any(c[1] == "/v1/escalations/raise" and "MISSING-TOOL" in c[2]["question"] for c in api.calls)
    transitions = [c[2]["to"] for c in api.calls if c[1].endswith("/transition")]
    assert transitions[-1] == "To Do" and "Needs owner" not in transitions
    assert not any("question for the owner" in str(c[2].get("body", "")) for c in api.calls)


def test_summary_naming_docker_or_php_and_missing_is_not_a_missing_tool(monkeypatch, tmp_path):
    result, api = _run_dev(monkeypatch, tmp_path, "DONE", "",
                           final_text="SUMMARY: Docker and PHP are installed; no missing checks remain.")
    assert result["outcome"] == "no_changes"
    assert "missing_tools" not in result.get("details", {})
    assert not any(call[1] == "/v1/escalations/raise" for call in api.calls)


def test_command_not_found_for_executed_catalogue_command_is_a_missing_tool(monkeypatch, tmp_path):
    result, _ = _run_dev(monkeypatch, tmp_path, "BLOCKED", "the check could not run",
                         command_results=[{"command": "php -v", "output": "bash: line 1: php: command not found",
                                           "exit_code": 127}])
    assert result["outcome"] == "tool_missing"
    assert result["details"]["missing_tools"] == ["php"]
    assert result["details"]["tool_missing_evidence"] == "command_not_found"
    assert result["details"]["command_not_found_tools"] == ["php"]


def test_quoted_command_error_in_blocked_explanation_is_not_execution_evidence(monkeypatch, tmp_path):
    reason = 'The docs show the example output "bash: php: command not found", but I did not run php.'
    result, _ = _run_dev(monkeypatch, tmp_path, "BLOCKED", reason)
    assert result["outcome"] == "blocked"
    assert result["details"].get("missing_tools", []) == []
    assert result["details"]["verdict_reason"] == reason


def test_compound_command_error_text_is_not_attributed_to_a_missing_tool(monkeypatch, tmp_path):
    result, _ = _run_dev(
        monkeypatch, tmp_path, "BLOCKED", "the command failed",
        command_results=[{"command": "echo 'bash: php: command not found'; php --bad 2>/dev/null",
                          "output": "bash: php: command not found", "exit_code": 1}],
    )
    assert result["outcome"] == "blocked"
    assert result["details"].get("missing_tools", []) == []


def test_unknown_missing_marker_fails_normally_and_keeps_the_ai_reason(monkeypatch, tmp_path):
    reason = "MISSING-TOOL: control - the check command cannot run"
    for word in ("control", "authenticated", "shared", "wordpress"):
        assert parse_missing_requirements(f"MISSING-TOOL: {word} - check cannot run") == []
    result, api = _run_dev(monkeypatch, tmp_path, "BLOCKED", reason)
    assert result["outcome"] == "failed"
    assert reason in result["summary"] and reason in result["details"]["verdict_reason"]
    assert not any(c[1] == "/v1/escalations/raise" for c in api.calls)
    assert [c[2]["to"] for c in api.calls if c[1].endswith("/transition")][-1] == "To Do"


def test_catalogue_includes_incident_tools_and_services_are_known():
    assert {"gitleaks", "xdebug", "cosign", "podman"}.issubset(CATALOG_TOOLS)
    assert {"gitleaks", "xdebug", "cosign", "podman", "postgres", "redis"}.issubset(known_requirements())


def test_environments_without_heartbeat_probes_are_not_accepted_requirements(ctx):
    from aiflow.v4.core.services import EXTERNAL_REQUIREMENTS

    for name in EXTERNAL_REQUIREMENTS:
        assert name not in known_requirements()
        assert name not in tooling.requirement_catalogue()
        assert parse_missing_requirements(f"MISSING-TOOL: {name} - check cannot run") == []


def test_missing_tool_marker_applies_a_register_answer_without_recording_a_wait(monkeypatch, tmp_path):
    register_answer = {
        "class": "decision_already_taken", "label": "Decision already taken",
        "reason": "the owner already decided this (No Docker for AI runs)", "resolved": True,
        "outcome": "auto_answered", "answer": "No. Docker is not used for AI runs.",
        "citation": 'Owner decision D1 "No Docker for AI runs"', "missing_tools": [],
    }
    result, api = _run_dev(monkeypatch, tmp_path, "BLOCKED",
                           "MISSING-TOOL: docker - Docker is not used for AI runs", register_answer)
    assert result["outcome"] == "auto_answered"
    assert result["details"]["escalation_class"] == "decision_already_taken"
    assert result["details"]["missing_tools"] == []
    comments = [call[2]["body"] for call in api.calls if call[1].endswith("/comment")]
    assert any("No. Docker is not used for AI runs." in body and "Owner decision D1" in body for body in comments)
    assert not any("waiting for a machine with the tool" in body for body in comments)
    transitions = [call[2]["to"] for call in api.calls if call[1].endswith("/transition")]
    assert transitions[-1] == "To Do"


def test_a_real_requirement_conflict_still_reaches_the_owner(monkeypatch, tmp_path):
    result, api = _run_dev(monkeypatch, tmp_path, "BLOCKED", "AC2 contradicts AC4; which one wins?")
    assert result["outcome"] == "blocked"
    assert [c[2]["to"] for c in api.calls if c[1].endswith("/transition")][-1] == "Needs owner"


def test_result_records_requirement_and_machine_and_ticket_stays_out_of_needs_owner(client, ctx):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    add_ticket(ctx.db, "TESTWP-1")
    run = claim_and_report_missing(client, cc, "TESTWP-1")
    wait = ctx.db.one("SELECT * FROM tool_waits WHERE ticket_key='TESTWP-1'")
    assert (wait["requirement"], wait["machine"], wait["project"], wait["run_id"]) == ("php", "cc", "TESTWP", run["run_id"])
    assert wait["resolved_at"] is None
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-1'")["status"] == "To Do"
    # nobody is asked: the master has nothing to triage
    assert [t for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]
            if t["step"] == "triage"] == []
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='ticket.waiting_for_tool'")["n"] == 1


def test_legacy_unknown_waits_are_ignored_and_closed_by_the_watchdog(client, ctx):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    add_ticket(ctx.db, "TESTWP-3")
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine,run_id)"
               " VALUES ('TESTWP-3','TESTWP','wordpress','cc','old-run')")
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]
    assert "TESTWP-3" in [t["key"] for t in candidates]
    assert all(wait["requirement"] != "wordpress" for wait in client.get("/v1/state").json()["tool_waits"])
    tooling.sweep(ctx)
    saved = ctx.db.one("SELECT resolved_at,resolution FROM tool_waits WHERE ticket_key='TESTWP-3'")
    assert saved["resolved_at"] is not None and saved["resolution"] == "unknown requirement ignored"


def test_requirement_reported_present_by_its_machine_is_failed_without_a_wait(client, ctx):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, {**CC_TOOLS, "docker": "Docker version 29.1.3"})
    add_ticket(ctx.db, "TESTWP-4")
    run = claim_and_report_missing(client, cc, "TESTWP-4", tools=("docker",))
    assert ctx.db.one("SELECT count(*) AS n FROM tool_waits WHERE ticket_key='TESTWP-4'")["n"] == 0
    saved = ctx.db.one("SELECT outcome,summary,details FROM runs WHERE id=%s", (run["run_id"],))
    assert saved["outcome"] == "failed" and "phpunit cannot run" in saved["summary"]
    assert saved["details"]["missing_tools"] == []


def test_unreportable_environments_are_failed_without_recording_waits(client, ctx):
    from aiflow.v4.core.services import EXTERNAL_REQUIREMENTS

    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    for index, name in enumerate(EXTERNAL_REQUIREMENTS, start=1):
        ticket = f"TESTWP-{30 + index}"
        add_ticket(ctx.db, ticket)
        report = f"MISSING-TOOL: {name} - the check cannot run"
        run = claim_and_report_missing(client, cc, ticket, tools=(name,), summary=report)

        saved = ctx.db.one("SELECT outcome,summary,details FROM runs WHERE id=%s", (run["run_id"],))
        assert saved["outcome"] == "failed"
        assert saved["summary"] == report
        assert saved["details"]["missing_tools"] == []
        assert ctx.db.one("SELECT count(*) AS n FROM tool_waits WHERE ticket_key=%s", (ticket,))["n"] == 0


# --- AC2: waiting state, offered again to a capable machine, counted in Command Center -------------------------
def test_ticket_waits_for_a_machine_with_the_tool_and_is_offered_again(client, ctx):
    cc, node1 = join(client, "cc"), join(client, "node1")
    heartbeat(client, "cc", cc, CC_TOOLS)
    heartbeat(client, "node1", node1, {**NODE1_TOOLS, "php": "missing"})
    add_ticket(ctx.db, "TESTWP-1")
    add_ticket(ctx.db, "TESTWP-2")
    claim_and_report_missing(client, cc, "TESTWP-1")

    def offered(account, headers):
        return [t["key"] for t in client.get("/v1/work/candidates", params={"account": account}, headers=headers).json()["tickets"]]

    assert offered("claude-01", cc) == ["TESTWP-2"]
    assert offered("codex-01", node1) == ["TESTWP-2"]  # node1 has no PHP either yet

    heartbeat(client, "node1", node1, NODE1_TOOLS)  # a machine with PHP is available
    assert "TESTWP-1" in offered("codex-01", node1)
    assert "TESTWP-1" not in offered("claude-01", cc)
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "codex-01"}, headers=node1).json()
    assert r["claimed"]
    assert ctx.db.one("SELECT resolution, resolved_at FROM tool_waits WHERE ticket_key='TESTWP-1'")["resolution"] == "offered to node1"


def test_missing_catalogue_tool_is_installed_from_heartbeat_request_and_wait_resolves(client, ctx, monkeypatch):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, {**CC_TOOLS, "gitleaks": "missing"})
    add_ticket(ctx.db, "TESTWP-5")
    claim_and_report_missing(client, cc, "TESTWP-5", tools=("gitleaks",))

    desired = heartbeat(client, "cc", cc, {**CC_TOOLS, "gitleaks": "missing"})["desired"]
    assert desired["tool_repairs"] == [{"name": "gitleaks", "version": ""}]
    available = {"git": "git version 2.43", "uv": "uv 0.5", "gitleaks": "missing"}
    installed = []

    def report(names=None):
        names = names or available
        return {name: available.get(name, "missing") for name in names}

    def install(name, version, packages):
        installed.append((name, version))
        available[name] = "gitleaks version: 8.25.1"

    monkeypatch.setattr(agent, "tool_versions", report)
    monkeypatch.setattr(operations, "_install_component", install)
    prepared = operations.prepare_project_toolchains(desired["project_stacks"], desired["versions"],
                                                      desired["tool_repairs"])
    assert installed == [("gitleaks", "")]
    assert prepared["request_gaps"] == []
    heartbeat(client, "cc", cc, {**CC_TOOLS, "gitleaks": available["gitleaks"]})
    wait = ctx.db.one("SELECT resolved_at,resolution FROM tool_waits WHERE ticket_key='TESTWP-5'")
    assert wait["resolved_at"] is not None and wait["resolution"] == "available on cc"


def test_command_center_counts_tickets_waiting_for_each_tool(client, ctx):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    for key in ("TESTWP-1", "TESTWP-2", "TESTWP-3"):
        add_ticket(ctx.db, key)
    claim_and_report_missing(client, cc, "TESTWP-1")
    claim_and_report_missing(client, cc, "TESTWP-2")
    claim_and_report_missing(client, cc, "TESTWP-3", tools=("playwright",))
    waits = {w["requirement"]: w for w in client.get("/v1/state").json()["tool_waits"]}
    assert waits["php"]["count"] == 2 and waits["playwright"]["count"] == 1
    assert sorted(t["key"] for t in waits["php"]["tickets"]) == ["TESTWP-1", "TESTWP-2"]
    assert waits["php"]["projects"] == ["TESTWP"] and waits["php"]["machines"] == ["cc"]
    assert waits["php"]["tickets"][0]["requirement"] == "php"
    assert waits["php"]["tickets"][0]["since"] and "heartbeat" in waits["php"]["action"]
    html = Path(tooling.__file__).with_name("ui").joinpath("console.html").read_text()
    assert "Parked tickets" in html and "Parked since" in html and "System action" in html and "tool_waits" in html


# --- AC3: one maintenance ticket after 24 hours without any machine or declared stack --------------------------
def _park(client, ctx, hours):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    for key in ("TESTWP-1", "TESTWP-2"):
        add_ticket(ctx.db, key)
        claim_and_report_missing(client, cc, key)
    ctx.db.run("UPDATE tool_waits SET created_at = now() - make_interval(hours => %s)", (hours,))
    return cc


def test_tooling_ticket_opens_once_after_24_hours(client, ctx):
    _park(client, ctx, 23)
    assert tooling.sweep(ctx) == [] and not getattr(ctx.jira, "created", [])
    ctx.db.run("UPDATE tool_waits SET created_at = now() - interval '25 hours'")
    ctx.db.run("UPDATE tooling_gaps SET since = now() - interval '25 hours'")
    opened = tooling.sweep(ctx)
    assert len(opened) == 1
    issue = ctx.jira.created[0]
    assert issue["project"] == "JAIF" and issue["labels"] == ["tooling"]
    assert "php" in issue["summary"]
    assert "TESTWP-1" in issue["description"] and "TESTWP-2" in issue["description"] and "TESTWP" in issue["description"]
    assert tooling.sweep(ctx) == [] and len(ctx.jira.created) == 1  # never a second ticket for the same tool
    assert client.get("/v1/state").json()["tool_waits"][0]["maintenance_ticket"] == opened[0]


def test_no_tooling_ticket_while_an_online_machine_has_the_tool(client, ctx):
    _park(client, ctx, 30)
    node1 = join(client, "node1")
    heartbeat(client, "node1", node1, NODE1_TOOLS)
    assert tooling.sweep(ctx) == [] and not getattr(ctx.jira, "created", [])
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '10 minutes' WHERE name='node1'")  # gone offline
    ctx.db.run("DELETE FROM tooling_gaps")
    # The fresh capable heartbeat resolved the ticket wait automatically; going offline later does not reopen it.
    assert tooling.sweep(ctx) == []


def test_no_tooling_ticket_while_a_project_stack_declares_the_tool(client, ctx, monkeypatch):
    _park(client, ctx, 30)
    monkeypatch.setattr(tooling, "declared_requirements", lambda repo: {"php", "composer"})
    assert tooling.sweep(ctx) == [] and not getattr(ctx.jira, "created", [])
    assert ctx.db.one("SELECT count(*) AS n FROM tooling_gaps")["n"] == 0  # the 24 hour clock restarts


def test_done_tickets_stop_waiting(client, ctx):
    _park(client, ctx, 30)
    ctx.db.run("UPDATE tickets SET status='Done'")
    assert tooling.sweep(ctx) == []
    assert ctx.db.one("SELECT count(*) AS n FROM tool_waits WHERE resolved_at IS NULL")["n"] == 0


def test_a_wait_without_progress_for_more_than_two_hours_warns_and_closes_on_resolution(client, ctx):
    cc = _park(client, ctx, 2)
    ctx.db.run("UPDATE tool_waits SET created_at = now() - interval '2 hours 1 minute'")
    tooling.check_stalled_waits(ctx)
    incidents = ctx.db.all("SELECT severity,title,detail FROM incidents WHERE kind='ticket_stuck'"
                           " AND subject LIKE 'tool_wait:%' AND closed_at IS NULL")
    assert len(incidents) == 2 and all(i["severity"] == "warning" and "php" in i["detail"] for i in incidents)
    heartbeat(client, "cc", cc, {**CC_TOOLS, "php": "PHP 8.3.1"})
    tooling.check_stalled_waits(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='ticket_stuck'"
                      " AND subject LIKE 'tool_wait:%' AND closed_at IS NULL")["n"] == 0


# --- AC4: check commands on a clean checkout, reported on the Projects screen ----------------------------------
def test_checks_are_asked_once_per_machine_and_reported_on_the_projects_screen(client, ctx):
    cc = join(client, "cc")
    desired = heartbeat(client, "cc", cc, CC_TOOLS)["desired"]
    (ask,) = desired["check_probes"]
    assert ask["project"] == "TESTWP"
    assert ask["repositories"] == [{"repo": "jooservices/test-aiflow-wp", "base": "develop",
                                    "checks": ["composer install", "vendor/bin/phpunit"]}]
    token = client.post("/v1/nodes/cc/check-probes/token", json={"project": "TESTWP", "repo": "jooservices/test-aiflow-wp"},
                        headers=cc)
    assert token.status_code == 200 and token.json()["token"]
    assert client.post("/v1/nodes/cc/check-probes/token", json={"project": "TESTWP", "repo": "other/repo"},
                       headers=cc).status_code == 403
    assert client.get("/v1/projects").json()["projects"][0]["checks"]["machines"] == []

    results = [{"repo": "jooservices/test-aiflow-wp", "command": "composer install", "status": "cannot_run",
                "tool": "composer", "detail": "composer is not installed"},
               {"repo": "jooservices/test-aiflow-wp", "command": "vendor/bin/phpunit", "status": "ran", "detail": "exit 0"}]
    assert client.post("/v1/nodes/cc/check-probes", json={"project": "TESTWP", "signature": "stale", "results": results},
                       headers=cc).status_code == 409
    saved = client.post("/v1/nodes/cc/check-probes", json={"project": "TESTWP", "signature": ask["signature"],
                                                           "results": results}, headers=cc)
    assert saved.json() == {"saved": True, "cannot_run": 1}
    assert heartbeat(client, "cc", cc, CC_TOOLS)["desired"]["check_probes"] == []  # once

    checks = client.get("/v1/projects").json()["projects"][0]["checks"]
    assert checks["cannot_run"] == 1
    (machine,) = checks["machines"]
    assert machine["node"] == "cc" and machine["current"] and machine["cannot_run"][0]["tool"] == "composer"

    node1 = join(client, "node1")  # a machine that joins later runs them again
    assert [p["project"] for p in heartbeat(client, "node1", node1, NODE1_TOOLS)["desired"]["check_probes"]] == ["TESTWP"]


def test_changed_check_commands_are_probed_again(client, ctx):
    cc = join(client, "cc")
    (ask,) = heartbeat(client, "cc", cc, CC_TOOLS)["desired"]["check_probes"]
    client.post("/v1/nodes/cc/check-probes", json={"project": "TESTWP", "signature": ask["signature"], "results": []},
                headers=cc)
    ctx.repo().projects["TESTWP"].checks.append("npm test")
    (again,) = heartbeat(client, "cc", cc, CC_TOOLS)["desired"]["check_probes"]
    assert again["signature"] != ask["signature"]
    checks = client.get("/v1/projects").json()["projects"][0]["checks"]
    assert checks["machines"][0]["current"] is False


def test_projects_screen_shows_checks_that_cannot_run():
    html = Path(tooling.__file__).with_name("ui").joinpath("console.html").read_text()
    assert "projectChecksCell" in html and "cannot run" in html and "<th>Checks</th>" in html


def test_node_probe_tells_a_missing_tool_from_a_failing_check(tmp_path):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8"}
    missing = probes.probe_command("definitely-not-installed-tool --version", tmp_path, env, [])
    assert missing["status"] == "cannot_run" and missing["tool"] == "definitely-not-installed-tool"
    inner = probes.probe_command("sh -c 'definitely-not-installed-tool2'", tmp_path, env, [])
    assert inner["status"] == "cannot_run" and inner["tool"] == "definitely-not-installed-tool2"
    assert probes.probe_command("false", tmp_path, env, [])["status"] == "ran"  # a failing check still ran
    assert probes.probe_command("FOO=1 true", tmp_path, env, [])["status"] == "ran"
    assert probes.command_tool("FOO=1 vendor/bin/phpunit") == "" and probes.command_tool("FOO=1 php -v") == "php"


def test_node_probe_clones_clean_and_reports_each_command(monkeypatch, tmp_path):
    repo = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "develop", str(repo)], check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.t", "commit", "-q", "-m", "i"],
                   check=True)

    class FakeGit:
        def __init__(self, *_a, **_k):
            pass

        def mkdirs(self, path):
            Path(path).mkdir(parents=True, exist_ok=True)

        def run(self, *args, token="", **_k):
            assert token == "ghs"
            subprocess.run(["git", "clone", "-q", "--branch", args[args.index("--branch") + 1], str(repo), args[-1]],
                           check=True)

    class Api:
        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None, retries=0):
            self.calls.append((path, body))
            return {"token": "ghs"}

    monkeypatch.setattr(probes, "Git", FakeGit)
    api = Api()
    item = {"project": "TESTWP", "signature": "sig", "repositories": [
        {"repo": "jooservices/test-aiflow-wp", "base": "develop", "checks": ["test -f README.md", "no-such-tool-xyz"]}]}
    assert probes.run_check_probes(api, "cc", [item], str(tmp_path / "srv"), []) == ["TESTWP"]
    path, body = api.calls[-1]
    assert path == "/v1/nodes/cc/check-probes" and body["signature"] == "sig"
    assert [(r["command"], r["status"]) for r in body["results"]] == [("test -f README.md", "ran"),
                                                                      ("no-such-tool-xyz", "cannot_run")]
    assert not list((tmp_path / "srv" / "work" / ".probe").glob("checks-*"))  # scratch checkout removed


# --- AC5: the developer AI is told which tools its machine has -------------------------------------------------
def test_claim_tells_the_developer_which_tools_the_machine_has(client, ctx):
    cc = join(client, "cc")
    heartbeat(client, "cc", cc, CC_TOOLS)
    add_ticket(ctx.db, "TESTWP-1")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).json()
    tools = r["machine_tools"]
    assert tools["available"]["git"] == "git version 2.43" and "php" in tools["missing"] and "php" not in tools["available"]
    claimed = _claimed()
    claimed.machine_tools = tools
    prompt = build_prompt(claimed, "TESTWP-1-s")
    assert "Tools on this machine" in prompt and "available: " in prompt and "git (git version 2.43)" in prompt
    assert "not available:" in prompt and "php" in prompt.split("not available:")[1].splitlines()[0]
    assert "MISSING-TOOL" in prompt and "Never ask the owner" in prompt
    assert machine_block({}) == ""


# --- AC6: the owner is asked only about real decisions ----------------------------------------------------------
def test_prompts_limit_owner_questions_to_requirement_infrastructure_and_release():
    root = Path(__file__).resolve().parents[2] / "prompts" / "v4"
    dev_prompt, triage = (root / "dev.md").read_text(), (root / "triage.md").read_text()
    for text in (dev_prompt, triage):
        assert "requirement conflict" in text and "infrastructure change" in text and "production release" in text
    assert "MISSING-TOOL" in dev_prompt and "never escalate" in triage
