"""Tickets carry requirements only; servers, hosts and access live in the project configuration (docs/v4/environment.md)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import add_ticket, join_node
from test_intake import REWRITE, Api, FakeAdapter, FakeGit, claim, iclient, run_worker  # noqa: F401 - fixtures
from test_projects import owner_sign_in, prepare_project_control
from aiflow.v4.control import api as control_api, environment as environment_reports, escalations
from aiflow.v4.core import settings as core_settings
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.core.environment import (find_server_wording, items_of, parse_items, parse_missing_environment, redact)
from aiflow.v4.worker import dev, intake, verify
from aiflow.v4.worker.dev import Claimed

ENVIRONMENT = [
    {"name": "WP_BASE_URL", "kind": "base_url", "value": "http://site.example.test:8080", "runs": ["dev", "verify"]},
    {"name": "WP_DB_NAME", "kind": "database", "value": "wp_test", "runs": ["dev"]},
    {"name": "BROWSERS", "kind": "browser", "value": "chromium,firefox", "runs": ["verify"]},
    {"name": "WP_ADMIN_PASSWORD", "kind": "secret", "runs": ["dev", "verify"]},
]
SECRET = "s3cret-value-from-the-machine"


@pytest.fixture
def envrepo(ctx, control_repo):
    """The control repo whose TESTWP project has an environment section."""
    path = control_repo / "projects" / "TESTWP.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["environment"] = ENVIRONMENT
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    ctx.repo = lambda: ControlRepo.load(control_repo)
    return control_repo


# --- AC1: the template has no section for machines, hosts, outside paths, accounts or Jira actions ---------------
def test_the_ticket_template_has_no_section_for_servers_or_access():
    forbidden = ("machine", "host", "server", "path", "account", "jira", "network", "access", "credential")
    for issue_type in intake.ISSUE_TYPE_SECTIONS:
        for heading in intake.required_sections(issue_type):
            assert not any(word in heading.lower() for word in forbidden), (issue_type, heading)
    # the bug's Environment section is about the product and client versions, not a machine
    assert "never a machine, host, network or account" in intake._outline_section("Environment:")


# --- AC2: the check that marks a ticket ready ----------------------------------------------------------------------
HOSTY = REWRITE.replace("Technical notes: WordPress theme template.",
                        "Technical notes: Run the tests on the shared Docker at 10.0.0.5 via ssh into the Dev VM.")


def test_wording_that_names_a_host_ip_machine_network_or_access_is_found():
    cases = {
        "Use the Dev VM for the tests": "machine", "the lab machines have it": "machine", "run it on WSL": "machine",
        "use the shared Docker": "machine", "connect to 192.168.1.20": "ip address",
        "browse http://wp.office.local:8080/": "host", "through the VPN": "network", "ssh into the box": "access instruction",
        "password: hunter22": "access instruction", "write a Jira comment when done": "jira action",
    }
    for text, kind in cases.items():
        assert kind in {f["kind"] for f in find_server_wording(text)}, text
    for requirement in ("Tests need a seeded site", "Visitors can add a comment", "PHP 8.3.36 is required",
                        "The page lists the projects at /portfolio"):
        assert find_server_wording(requirement) == [], requirement
    assert redact("run it on the Dev VM now") == "run it on the [server detail removed] now"


def test_a_ticket_naming_a_host_is_rejected_and_the_same_requirement_without_it_is_accepted():
    kind, why = intake.parse(f"DESCRIPTION:\n<<<\n{HOSTY}\n>>>\nINTAKE: REWRITE")
    assert kind == "invalid"
    assert "describe the requirement instead" in why and "10.0.0.5" in why and "project configuration" in why
    assert intake.readiness_problems(HOSTY) and not intake.readiness_problems(REWRITE)
    assert intake.parse(f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE") == ("rewrite", REWRITE)


def test_intake_run_rejects_a_host_in_the_rewrite_and_writes_nothing(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-40", status="Intake", labels=())
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-40",
                        f"DESCRIPTION:\n<<<\n{HOSTY}\n>>>\nINTAKE: REWRITE\n")
    assert res["outcome"] == "failed" and "describe the requirement instead" in res["summary"]
    assert getattr(ctx.jira, "descriptions", []) == [] and not [c for c in ctx.jira.comments if c[0] == "TESTWP-40"]
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-40'")["status"] == "Intake"


# --- AC3/AC4: wording removed, recorded, and a real need becomes an environment ticket ------------------------------
def _rewrite_with_removal(iclient, ctx, tmp_path, monkeypatch, key, description):
    add_ticket(ctx.db, key, status="Intake", labels=())
    ctx.db.run("UPDATE tickets SET description=%s WHERE key=%s", (description, key))
    answer = ("REMOVED:\n- machine: where the tests run -> the tests run against an isolated seeded site\n"
              "ENVIRONMENT NEEDS:\n- tests need a seeded site\n"
              f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE\n")
    return run_worker(iclient, ctx, tmp_path, monkeypatch, key, answer)


def test_removed_wording_is_recorded_and_the_real_need_opens_one_environment_ticket(iclient, ctx, tmp_path, monkeypatch):
    original = "Portfolio page. Run the tests on the shared Docker at 10.0.0.5, the Dev VM; they need a seeded site."
    res, _ = _rewrite_with_removal(iclient, ctx, tmp_path, monkeypatch, "TESTWP-41", original)
    assert res["outcome"] == "refined"
    assert ctx.jira.descriptions[-1] == ("TESTWP-41", REWRITE) and find_server_wording(REWRITE) == []
    comment = [t for k, t in ctx.jira.comments if k == "TESTWP-41"][0]
    assert "10.0.0.5" not in comment and "shared Docker" not in comment and "Dev VM" not in comment
    assert "Removed from the ticket: machine, ip address" in comment and "isolated seeded site" in comment
    created = ctx.jira.created
    assert len(created) == 1 and created[0]["project"] == "JAIF" and created[0]["labels"] == ["environment"]
    assert "seeded site" in created[0]["summary"]
    assert (created[0]["key"], "TESTWP-41", "Relates") in ctx.jira.issue_links
    assert f"Environment need: tests need a seeded site -> {created[0]['key']} (opened)" in comment
    record = ctx.db.one("SELECT removed, needs FROM environment_removals WHERE ticket_key='TESTWP-41'")
    assert "ip address: 10.0.0.5" in record["removed"] and record["needs"] == ["tests need a seeded site"]
    # the same need on another story links to the same environment ticket
    res2, _ = _rewrite_with_removal(iclient, ctx, tmp_path, monkeypatch, "TESTWP-42", original)
    assert res2["outcome"] == "refined" and len(ctx.jira.created) == 1
    assert (created[0]["key"], "TESTWP-42", "Relates") in ctx.jira.issue_links
    row = ctx.db.one("SELECT linked_tickets FROM environment_needs")
    assert sorted(row["linked_tickets"]) == ["TESTWP-41", "TESTWP-42"]


def test_an_ordinary_rewrite_records_nothing_about_the_environment(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-43", status="Intake", labels=())
    run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-43", f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE\n")
    assert ctx.db.one("SELECT count(*) AS n FROM environment_removals")["n"] == 0 and not getattr(ctx.jira, "created", [])


# --- AC5: the environment section, on a screen and in a file ---------------------------------------------------------
def test_environment_section_is_validated_secrets_are_by_name_only():
    items, problems = parse_items(ENVIRONMENT)
    assert problems == [] and [i.name for i in items] == ["WP_BASE_URL", "WP_DB_NAME", "BROWSERS", "WP_ADMIN_PASSWORD"]
    assert items[3].public() == {"name": "WP_ADMIN_PASSWORD", "kind": "secret", "value": "", "runs": ["dev", "verify"],
                                 "description": ""}
    for bad, fragment in (
            ({"name": "x", "kind": "secret"}, "environment variable name"),
            ({"name": "TOKEN", "kind": "secret", "value": "abc"}, "by name only"),
            ({"name": "DB_URL", "kind": "database", "value": "postgres://u:pw@db/x"}, "carries a credential"),
            ({"name": "URL", "kind": "base_url"}, "needs a value"),
            ({"name": "URL", "kind": "wiki", "value": "x"}, "kind must be one of"),
            ({"name": "URL", "kind": "base_url", "value": "x", "runs": ["deploy"]}, "runs may only be")):
        assert fragment in parse_items([bad])[1][0], bad


def test_projects_screen_saves_the_environment_to_the_control_repository(client, ctx, control_repo):
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    saved = client.put("/v1/projects/TESTWP/environment", json={"items": ENVIRONMENT})
    assert saved.status_code == 200, saved.text
    config = yaml.safe_load((control_repo / "projects/TESTWP.yaml").read_text(encoding="utf-8"))
    assert [i["name"] for i in config["environment"]] == ["WP_BASE_URL", "WP_DB_NAME", "BROWSERS", "WP_ADMIN_PASSWORD"]
    assert "value" not in config["environment"][3] and ctx.github.commits[-1]["path"] == "projects/TESTWP.yaml"
    loaded = client.get("/v1/projects/TESTWP/environment").json()
    assert [i["name"] for i in loaded["items"]] == [i["name"] for i in ENVIRONMENT] and loaded["problems"] == []
    audit = ctx.db.one("SELECT accepted, payload FROM write_audit WHERE action='save_project_environment' ORDER BY id DESC")
    assert audit["accepted"] and "10.0" not in str(audit["payload"]) and "site.example.test" not in str(audit["payload"])
    refused = client.put("/v1/projects/TESTWP/environment",
                         json={"items": [{"name": "TOKEN", "kind": "secret", "value": "leak"}]})
    assert refused.status_code == 422 and "by name only" in refused.json()["detail"]
    assert client.put("/v1/projects/NOPE/environment", json={"items": []}).status_code == 404
    client.cookies.clear()
    assert client.put("/v1/projects/TESTWP/environment", json={"items": []}).status_code == 401


def test_the_console_has_the_environment_screens():
    html = (control_api.Path(control_api.__file__).parent / "ui" / "console.html").read_text(encoding="utf-8")
    for needle in ("project-environment", "Missing environment items", "Tickets that name servers", "/v1/environment/scan"):
        assert needle in html


# --- AC6: the run receives the items as environment variables, the notes list their names ---------------------------
class _CaptureAdapter:
    def __init__(self, verdict="DONE", reason=""):
        self.verdict, self.reason, self.spec = verdict, reason, None

    def argv(self, _spec):
        return []

    def run(self, spec, *_a, **_k):
        self.spec = spec
        return SimpleNamespace(tokens_in=1, tokens_out=1, cost_usd=0, quota={}, error_kind="", reset_at=None,
                               verdict=self.verdict, verdict_reason=self.reason, error_message="", final_text="",
                               permission_denials=[], exit_code=0, duration_s=1, log_path="")


class _Api:
    node_name = "cc"

    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path == "/v1/escalations/raise":
            return {"class": "new_business_decision", "label": "x", "reason": "x", "resolved": False,
                    "outcome": "blocked", "answer": "", "citation": "", "missing_tools": []}
        return {"token": "t", "url": "https://github.com/x/y/pull/1", "number": 1}


class _Git:
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


def _claimed():
    project = {"key": "TESTWP", "repo": "jooservices/test-aiflow-wp", "base": "develop", "checks": ["vendor/bin/phpunit"],
               "work_minutes": {"hard": 60}, "merge": {"method": "squash"},
               "environment": [i.public() for i in items_of(ENVIRONMENT)]}
    return Claimed(run_id="run-1", fence=3, run_token="t", account="claude-01", provider="claude", model="opus",
                   effort="", ticket={"key": "TESTWP-1", "project": "TESTWP", "summary": "S", "description": "AC1",
                                      "labels": [], "priority": "Medium", "type": "Story"},
                   project=project, identity=("bot", "bot@example.test"))


def _run_dev(monkeypatch, tmp_path, verdict="DONE", reason=""):
    adapter = _CaptureAdapter(verdict, reason)
    monkeypatch.setattr(dev, "Git", _Git)
    monkeypatch.setattr(dev, "get_adapter", lambda _p: adapter)
    monkeypatch.setattr(core_settings, "read_env_file",
                        lambda path: {"WP_ADMIN_PASSWORD": SECRET, "OTHER": "kept"} if str(path).endswith("TESTWP.env") else {})
    api = _Api()
    return dev.run_dev(api, _claimed(), str(tmp_path), "https://control", 3600, []), api, adapter


def test_environment_variables_reach_the_run_and_the_notes_list_their_names(monkeypatch, tmp_path):
    _result, _api, adapter = _run_dev(monkeypatch, tmp_path)
    env = adapter.spec.extra_env
    assert env["WP_BASE_URL"] == "http://site.example.test:8080" and env["WP_DB_NAME"] == "wp_test"
    assert env["WP_ADMIN_PASSWORD"] == SECRET and env["OTHER"] == "kept"  # a secret comes from the machine, by name
    assert "BROWSERS" not in env  # this item is for verify runs only
    prompt = adapter.spec.prompt
    for name in ("WP_BASE_URL", "WP_DB_NAME", "WP_ADMIN_PASSWORD"):
        assert f"- {name} (" in prompt
    assert "- BROWSERS (" not in prompt
    assert SECRET not in prompt and "site.example.test" not in prompt  # names only
    assert "MISSING-ENV" in prompt and "never ask the owner" in prompt.lower()


def test_verify_runs_get_the_items_allowed_for_verify(monkeypatch):
    monkeypatch.setattr(core_settings, "read_env_file", lambda _p: {"WP_ADMIN_PASSWORD": SECRET})
    from aiflow.v4.worker.notes import project_env
    env = project_env("TESTWP", [i.public() for i in items_of(ENVIRONMENT)], "verify")
    assert env["BROWSERS"] == "chromium,firefox" and env["WP_BASE_URL"] and "WP_DB_NAME" not in env
    prompt = verify.build_prompt(_claimed(), {"number": 1, "branch": "b", "url": "u"}, env)
    assert "- BROWSERS (" in prompt and SECRET not in prompt


def test_the_claim_carries_the_environment_without_secret_values(client, ctx, envrepo):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-50")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01"}, headers=cc).json()
    items = {i["name"]: i for i in claimed["project"]["environment"]}
    assert items["WP_BASE_URL"]["value"] == "http://site.example.test:8080"
    assert items["WP_ADMIN_PASSWORD"]["kind"] == "secret" and items["WP_ADMIN_PASSWORD"]["value"] == ""


# --- AC8: a missing item is reported by name, recorded by name, the owner is not asked -------------------------------
def test_marker_accepts_only_names_from_the_configuration():
    items = items_of(ENVIRONMENT)
    assert parse_missing_environment("MISSING-ENV: WP_DB_NAME, wp_base_url - cannot run", items) == (
        ["WP_DB_NAME", "WP_BASE_URL"], [], True)
    assert parse_missing_environment("MISSING-ENV: the shared staging site", items)[:1] == ([],)
    assert parse_missing_environment("no marker here", items) == ([], [], False)


def test_dev_run_reports_a_missing_environment_item_by_name(monkeypatch, tmp_path):
    result, api, _ = _run_dev(monkeypatch, tmp_path, "BLOCKED", "MISSING-ENV: WP_DB_NAME - the test database is not there")
    assert result["outcome"] == "environment_missing" and result["details"]["missing_environment"] == ["WP_DB_NAME"]
    assert [c[2]["to"] for c in api.calls if c[1].endswith("/transition")][-1] == "To Do"
    assert not any(c[1] == "/v1/escalations/raise" for c in api.calls)
    assert not any("question for the owner" in str(c[2].get("body", "")) for c in api.calls)


def test_free_text_is_not_an_environment_item(monkeypatch, tmp_path):
    result, api, _ = _run_dev(monkeypatch, tmp_path, "BLOCKED", "MISSING-ENV: the shared staging site - cannot run")
    assert result["outcome"] == "failed" and "not free text" in result["summary"]
    assert [c[2]["to"] for c in api.calls if c[1].endswith("/transition")][-1] == "To Do"


def _report(client, headers, ticket, names, summary="the database is not there"):
    r = client.post("/v1/work/claim", json={"ticket": ticket, "account": "claude-01"}, headers=headers).json()
    assert r["claimed"], r
    done = client.post(f"/v1/runs/{r['run_id']}/result", json={
        "fence": r["fence"], "outcome": "environment_missing", "summary": summary,
        "details": {"missing_environment": names}}, headers=headers)
    assert done.status_code == 200, done.text
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=headers)
    return r


def test_the_report_is_recorded_by_name_and_the_ticket_waits_without_asking_the_owner(client, ctx, envrepo):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-51")
    run = _report(client, cc, "TESTWP-51", ["WP_DB_NAME", "NOT_IN_CONFIGURATION"])
    row = ctx.db.one("SELECT * FROM environment_reports WHERE ticket_key='TESTWP-51'")
    assert (row["item"], row["project"], row["machine"], row["run_id"]) == ("WP_DB_NAME", "TESTWP", "cc", run["run_id"])
    assert ctx.db.one("SELECT count(*) AS n FROM environment_reports")["n"] == 1  # the unknown name is not recorded
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-51'")["status"] == "To Do"
    offered = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]
    assert "TESTWP-51" not in [t["key"] for t in offered]  # parked until the item is provided
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='ticket.waiting_for_environment'")["n"] == 1
    owner_sign_in(client, ctx)
    shown = client.get("/v1/environment/missing").json()["missing"]
    assert [(m["item"], m["ticket"], m["state"]) for m in shown] == [("WP_DB_NAME", "TESTWP-51", "waiting")]


def test_an_unknown_name_alone_fails_the_run(client, ctx, envrepo):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-52")
    run = _report(client, cc, "TESTWP-52", ["NOT_IN_CONFIGURATION"])
    assert ctx.db.one("SELECT outcome FROM runs WHERE id=%s", (run["run_id"],))["outcome"] == "failed"
    assert ctx.db.one("SELECT count(*) AS n FROM environment_reports")["n"] == 0


def test_the_item_is_provided_or_one_environment_ticket_is_raised(client, ctx, envrepo, control_repo):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    for key in ("TESTWP-53", "TESTWP-54"):
        add_ticket(ctx.db, key)
        _report(client, cc, key, ["WP_DB_NAME"])
    assert environment_reports.sweep(ctx) == []  # not yet: the doctor may provide the item first
    ctx.db.run("UPDATE environment_reports SET created_at = now() - interval '2 hours'")
    opened = environment_reports.sweep(ctx)
    assert len(opened) == 1 and ctx.jira.created[0]["labels"] == ["environment"]
    assert "WP_DB_NAME" in ctx.jira.created[0]["summary"] and "TESTWP-53" in ctx.jira.created[0]["description"]
    assert find_server_wording(ctx.jira.created[0]["description"]) == []
    assert environment_reports.sweep(ctx) == [] and len(ctx.jira.created) == 1  # one ticket, never a second
    assert {r["environment_ticket"] for r in ctx.db.all("SELECT environment_ticket FROM environment_reports")} == {opened[0]}
    assert environment_reports.provide(ctx, "TESTWP", "WP_DB_NAME", "created by the flow doctor") == 2
    offered = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]
    assert {"TESTWP-53", "TESTWP-54"} <= {t["key"] for t in offered}


def test_changing_the_item_in_the_configuration_offers_the_ticket_again(client, ctx, envrepo, control_repo):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-55")
    _report(client, cc, "TESTWP-55", ["WP_DB_NAME"])
    path = control_repo / "projects" / "TESTWP.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["environment"][1]["value"] = "wp_test_2"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    environment_reports.sweep(ctx)
    assert ctx.db.one("SELECT resolution FROM environment_reports")["resolution"] == "the configuration of the item changed"


# --- AC9: the AI never asks the owner about servers --------------------------------------------------------------------
def test_a_question_about_a_server_is_answered_without_the_owner(client, ctx):
    cc = {"Authorization": "Bearer " + join_node(client, "cc").json()["node_token"]}
    add_ticket(ctx.db, "TESTWP-56")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-56", "account": "claude-01"}, headers=cc).json()
    raised = client.post("/v1/escalations/raise", json={
        "run_id": r["run_id"], "fence": r["fence"],
        "question": "Which server should I use for the integration site? Please give me the address and the login."},
        headers=cc).json()
    assert raised["class"] == "missing_tool_or_environment" and raised["resolved"] and raised["outcome"] == "auto_answered"
    assert "MISSING-ENV" in raised["answer"] and "environment variables" in raised["answer"]
    assert escalations.classify(ctx.db, "May I deploy to the production server now?", "TESTWP").cls == \
        escalations.IRREVERSIBLE  # a production action still reaches a person


# --- AC10: what only a particular machine can prove is verified after the merge -----------------------------------------
def test_prompts_leave_machine_bound_criteria_to_the_flow_after_the_merge(monkeypatch, tmp_path):
    _result, _api, adapter = _run_dev(monkeypatch, tmp_path)
    assert "verified by AI Flow after the merge (canary, release checks)" in adapter.spec.prompt.replace("\n", " ").replace("  ", " ")
    review = verify.build_prompt(_claimed(), {"number": 1, "branch": "b", "url": "u"}, {})
    assert "verified by AI Flow after the merge" in review.replace("\n", " ")


# --- AC11: the scan of the open tickets -----------------------------------------------------------------------------------
def test_scan_lists_open_tickets_that_break_the_rule_corrects_them_and_keeps_the_report(client, ctx):
    texts = {"TESTWP-60": ("To Do", "Run on the Dev VM at 10.1.2.3"), "TESTWP-61": ("To Do", "Visitors can sort the list"),
             "TESTWP-62": ("In Progress", "Use the shared Docker"), "TESTWP-63": ("Done", "Use the shared Docker"),
             "TESTWP-64": ("Intake", "ssh into the lab machines"), "TESTWP-65": ("Backlog", "Write a Jira comment when done")}
    for key, (status, description) in texts.items():
        add_ticket(ctx.db, key, status=status)
        ctx.db.run("UPDATE tickets SET description=%s WHERE key=%s", (description, key))
        ctx.jira.status_of[key] = status
    report = environment_reports.scan(ctx)
    by_ticket = {o["ticket"]: o for o in report["offenders"]}
    assert set(by_ticket) == {"TESTWP-60", "TESTWP-62", "TESTWP-64", "TESTWP-65"}  # done and clean ones are not listed
    assert report["scanned"] == 5
    assert set(by_ticket["TESTWP-60"]["kinds"]) == {"machine", "ip address"}
    assert by_ticket["TESTWP-60"]["action"] == "sent back to intake" and by_ticket["TESTWP-65"]["action"] == "sent back to intake"
    assert by_ticket["TESTWP-62"]["action"] == "work in flight; left as it is"
    assert by_ticket["TESTWP-64"]["action"] == "already in intake"
    row = ctx.db.one("SELECT status, labels FROM tickets WHERE key='TESTWP-60'")
    assert row["status"] == "Intake" and "aiflow-ready" not in row["labels"]
    assert ctx.jira.status_of["TESTWP-60"] == "Intake" and ctx.jira.status_of["TESTWP-62"] == "In Progress"
    note = [t for k, t in ctx.jira.comments if k == "TESTWP-60"][0]
    assert "10.1.2.3" not in note and "Dev VM" not in note and "machine" in note  # the note never repeats the wording
    assert environment_reports.last_scan(ctx)["offenders"] == report["offenders"]  # the report is kept
    assert environment_reports.scan_if_due(ctx) is None  # once a day


def test_scan_runs_from_the_projects_screen_and_the_report_is_read_back(client, ctx):
    owner_sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-70")
    ctx.db.run("UPDATE tickets SET description='use the VPN' WHERE key='TESTWP-70'")
    ctx.jira.status_of["TESTWP-70"] = "To Do"
    ran = client.post("/v1/environment/scan").json()["scan"]
    assert [o["ticket"] for o in ran["offenders"]] == ["TESTWP-70"] and ran["actor"] == "owner@example.test"
    assert client.get("/v1/environment/scan").json()["scan"]["offenders"] == ran["offenders"]
    client.cookies.clear()
    assert client.post("/v1/environment/scan").status_code == 401


# --- AC7: nothing about a server in a source repository --------------------------------------------------------------------
def test_the_run_prompts_name_no_server():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("dev.md", "verify.md"):  # intake.md quotes examples of the wording it removes
        assert find_server_wording((root / "prompts" / "v4" / name).read_text(encoding="utf-8")) == [], name
