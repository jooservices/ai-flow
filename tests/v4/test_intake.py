"""Intake step: Intake tickets go to intake_refiner accounts, the worker rewrites them into the template through
the fenced description endpoint, or sends questions to the owner."""
from pathlib import Path
import json

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.adapters import RunResult
from aiflow.v4.control.api import create_app
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.worker import intake
from aiflow.v4.worker.dev import Claimed

REWRITE = """Business requirement: Visitors can find the portfolio page.
Acceptance criteria:
- AC1: /portfolio lists the projects
Technical notes: WordPress theme template.
Definition of done:
- tests cover AC1"""
BUG_REWRITE = """Business requirement: Visitors need checkout to complete reliably.
Steps to reproduce: Submit a cart with a valid payment method.
Expected result: The order completes and a confirmation is shown.
Actual result: Checkout returns an error.
Environment: Production, current Chrome.
Acceptance criteria:
- AC1: A valid checkout completes.
Technical notes: Keep the payment integration backward compatible.
Definition of done:
- Tests cover AC1."""
EXPECTED_EXTRA_SECTIONS = {
    "Epic": (), "Story": (), "Feature": (), "Bug": ("Steps to reproduce:", "Expected result:", "Actual result:", "Environment:"),
    "Hotfix": ("Steps to reproduce:", "Expected result:", "Actual result:", "Environment:"),
    "Tech Debt": ("Problem:", "Risk:"), "Chore": (), "Spike": ("Question:", "Timebox:", "Output:"),
    "Docs": ("Reader:",), "Security": ("Threat:", "Impact:"), "Sub-task": (),
}


@pytest.fixture
def iclient(ctx, control_repo):
    """Control API whose control repo adds intake_refiner accounts on node cc."""
    with (control_repo / "accounts.yaml").open("a") as f:
        f.write("  - {id: grok-01, provider: grok, node: cc, tier: middle, roles: [intake_refiner]}\n"
                "  - {id: grok-02, provider: grok, node: cc, tier: middle, roles: [intake_refiner], explicit_only: true}\n")
    snap = ControlRepo.load(control_repo)
    ctx.repo = lambda: snap
    from fastapi.testclient import TestClient
    app = create_app(ctx)
    app.state.sync_accounts()
    c = TestClient(app)
    token = join_node(c, "cc").json()["node_token"]
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def candidates(client, account):
    return [(t["key"], t["step"]) for t in client.get("/v1/work/candidates", params={"account": account}).json()["tickets"]]


def claim(client, key, account="grok-01", step="intake"):
    r = client.post("/v1/work/claim", json={"ticket": key, "account": account, "step": step})
    assert r.status_code == 200, r.text
    return r.json()


# --- AC1: candidates ---------------------------------------------------------------------------------------
def test_intake_tickets_are_offered_to_intake_refiners_only(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-20", status="Intake", labels=())
    add_ticket(ctx.db, "TESTWP-21", status="Intake", labels=("aiflow-hold",))
    add_ticket(ctx.db, "TESTWP-22", status="To Do")

    assert candidates(iclient, "grok-01") == [("TESTWP-20", "intake")]
    assert ("TESTWP-20", "intake") not in candidates(iclient, "claude-01")  # dev/verify roles only
    assert candidates(iclient, "grok-02") == []  # explicit_only without its ai-grok label
    add_ticket(ctx.db, "TESTWP-20", status="Intake", labels=("ai-grok",))
    assert candidates(iclient, "grok-02") == [("TESTWP-20", "intake")]

    r = claim(iclient, "TESTWP-20")
    assert r["step"] == "intake" and r["ticket"]["description"].startswith("Acceptance criteria")
    assert candidates(iclient, "grok-01") == []  # held by the run
    # a dev claim of an Intake ticket is refused
    assert iclient.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "claude-01", "step": "dev"}).status_code == 409


def test_ready_hold_and_senior_labels_keep_their_existing_routing(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-32", status="To Do", labels=("aiflow-ready", "tier-senior"))
    add_ticket(ctx.db, "TESTWP-33", status="To Do", labels=("aiflow-ready", "tier-senior", "aiflow-hold"))

    assert ("TESTWP-32", "dev") not in candidates(iclient, "grok-01")  # middle-tier account
    assert ("TESTWP-32", "dev") in candidates(iclient, "claude-01")  # senior-tier account
    assert ("TESTWP-33", "dev") not in candidates(iclient, "claude-01")  # hold still takes precedence


def test_failed_intake_waits_before_the_next_try(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-23", status="Intake", labels=())
    r = claim(iclient, "TESTWP-23")
    iclient.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "failed", "summary": "no answer"})
    iclient.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]})
    assert candidates(iclient, "grok-01") == []
    ctx.db.run("UPDATE runs SET started_at = now() - interval '20 minutes', ended_at = now() - interval '11 minutes'")
    assert candidates(iclient, "grok-01") == [("TESTWP-23", "intake")]


# --- AC3/AC4: description endpoint and fencing ---------------------------------------------------------------
def test_description_endpoint_is_fenced_and_for_intake_runs_only(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-24", status="Intake", labels=())
    add_ticket(ctx.db, "TESTWP-25")
    r = claim(iclient, "TESTWP-24")
    body = {"run_id": r["run_id"], "fence": r["fence"], "body": REWRITE}

    assert iclient.post("/v1/jira/TESTWP-25/description", json=body).status_code == 403  # another ticket
    assert iclient.post("/v1/jira/TESTWP-24/description", json={**body, "body": "  "}).status_code == 400
    assert iclient.post("/v1/jira/TESTWP-24/description", json=body).json() == {"ok": True}
    assert ctx.jira.descriptions == [("TESTWP-24", REWRITE)]
    assert ctx.db.one("SELECT description FROM tickets WHERE key='TESTWP-24'")["description"] == REWRITE
    audit = ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='description'")
    assert audit["accepted"] and audit["result"] == "replaced"

    # the lease moves on: the old fence can no longer replace the description, and the refusal is audited
    ctx.leases.r.delete(f"{ctx.leases.p}lease:TESTWP-24")
    assert ctx.leases.claim("TESTWP-24", "other-run", 60_000).fence == r["fence"] + 1
    assert iclient.post("/v1/jira/TESTWP-24/description", json={**body, "body": "late"}).status_code == 409
    assert ctx.jira.descriptions == [("TESTWP-24", REWRITE)]
    assert ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE action='description' AND NOT accepted")["n"] == 1

    # a dev run holds a valid fence but may not rewrite the ticket
    d = claim(iclient, "TESTWP-25", account="claude-01", step="dev")
    assert iclient.post("/v1/jira/TESTWP-25/description",
                        json={"run_id": d["run_id"], "fence": d["fence"], "body": REWRITE}).status_code == 403


def test_intake_run_may_not_edit_files(iclient, ctx, tmp_path):
    add_ticket(ctx.db, "TESTWP-26", status="Intake", labels=())
    r = claim(iclient, "TESTWP-26")

    def decision(tool, cwd="/srv/aiflow/work/x", **tool_input):
        hook = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        out = iclient.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {r['run_token']}"})
        return out.json()["hookSpecificOutput"]["permissionDecision"]

    assert decision("Write", file_path="/srv/aiflow/work/x/a.txt") == "deny"
    assert decision("Edit", file_path="/srv/aiflow/work/x/a.txt") == "deny"
    # the project's allowlist holds installs and builds; an intake run may not use them
    for command in ("composer install", "npm install", "git checkout -b x", "echo hi > a.txt", "cat a | tee b",
                    "find . -name '*.php' -delete", "find . -exec rm {} ;", "sort -o out.txt in.txt",
                    "git diff --output=patch.txt", "ls $(rm -rf .)", "env rm a.txt", "rm a.txt"):
        assert decision("Bash", command=command) == "deny", command
    for command in ("ls -la", "cat docs/SPEC.md", "grep -rn 'Business requirement' docs | head -5",
                    "git log --oneline -5", "find . -name '*.php'"):
        assert decision("Bash", command=command) == "allow", command
    # Quoting must not hide find's destructive option, and the shell cannot start another command in the background.
    for command in ("find . '-delete'", "ls & rm -rf /tmp/intake-policy-test", "cat /etc/passwd",
                    "grep -f /etc/passwd docs/SPEC.md", "find /tmp -name '*.txt'", "cat ../outside.txt",
                    "rg --follow .", "grep -R 'private' .", "find -L . -name '*.txt'",
                    "date -f/etc/passwd"):
        assert decision("Bash", command=command) == "deny", command
    assert decision("Read", file_path="/srv/aiflow/work/x/docs/SPEC.md") == "allow"
    assert decision("Read", file_path="/etc/passwd") == "deny"
    assert decision("Glob", pattern="/etc/**") == "deny"
    assert decision("Grep", path="../../etc") == "deny"
    worktree, outside = tmp_path / "checkout", tmp_path / "outside"
    worktree.mkdir()
    outside.write_text("outside the checkout")
    (worktree / "link.txt").symlink_to(outside)
    assert decision("Read", cwd=str(worktree), file_path="link.txt") == "deny"
    assert decision("Glob", cwd=str(worktree), pattern="**/*") == "deny"
    assert decision("WebFetch", url="https://example.com") == "deny"
    assert decision("WebSearch", query="project docs") == "deny"
    # a dev run keeps the project's command allowlist
    add_ticket(ctx.db, "TESTWP-30")
    d = claim(iclient, "TESTWP-30", account="claude-01", step="dev")
    hook = {"tool_name": "Bash", "tool_input": {"command": "composer install"}, "cwd": "/srv/aiflow/work/x"}
    out = iclient.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {d['run_token']}"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


# --- AC2/AC3: the worker ------------------------------------------------------------------------------------
class Api:
    """ControlClient over the in-process control API."""
    node_name = "cc"

    def __init__(self, client):
        self.c = client

    def call(self, method, path, json=None, params=None):
        r = self.c.request(method, path, json=json, params=params)
        if r.status_code == 409:
            from aiflow.v4.worker.client import Conflict
            raise Conflict(r.text)
        assert r.status_code < 400, r.text
        return r.json()


class FakeGit:
    spec: str | None = "# Spec\nStack: WordPress. Every AC needs a PHPUnit test."

    def __init__(self, srv_root, run_as=None):
        pass

    def mirror(self, repo, token, _transport_url=""):
        return Path("/nonexistent/mirror.git")

    def detached(self, mirror, path, ref):
        (path / "docs").mkdir(parents=True, exist_ok=True)
        if self.spec is not None:
            (path / "docs" / "SPEC.md").write_text(self.spec)
        return "abc123def456"


class FakeAdapter:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def argv(self, spec):
        return ["fake"]

    def run(self, spec, on_progress=None, stop=None):
        self.prompts.append(spec.prompt)
        self.extra_env = spec.extra_env
        return RunResult(exit_code=0, final_text=self.answer, tokens_in=10, tokens_out=20)


def run_worker(iclient, ctx, tmp_path, monkeypatch, key, answer, spec=FakeGit.spec):
    monkeypatch.setattr(FakeGit, "spec", spec)
    adapter = FakeAdapter(answer)
    monkeypatch.setattr(intake, "Git", FakeGit)
    monkeypatch.setattr(intake, "get_adapter", lambda provider: adapter)
    r = claim(iclient, key)
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account=r["account"],
                provider=r["provider"], model=r["model"], effort=r["effort"], ticket=r["ticket"], project=r["project"],
                identity=tuple(r["identity"]), step="intake", recent_comments=r.get("recent_comments") or [])
    res = intake.run_intake(Api(iclient), c, str(tmp_path), "http://control", 3600, [])
    iclient.post(f"/v1/runs/{c.run_id}/result", json=res)
    return res, adapter


def test_rewrite_keeps_the_original_and_queues_the_ticket(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-27", status="Intake", labels=())
    ctx.db.run("UPDATE tickets SET description='make a portfolio page pls' WHERE key='TESTWP-27'")
    answer = f"I rewrote it.\nDESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE\n"
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-27", answer)

    assert res["outcome"] == "refined"
    prompt = adapter.prompts[0]
    assert "make a portfolio page pls" in prompt and "Every AC needs a PHPUnit test." in prompt  # AC2
    assert "Jira issue type: Task. Use the Story template." in prompt  # missing Jira types still default to Task
    assert "Business requirement:" in prompt and "- AC1:" in prompt
    assert adapter.extra_env == {}  # the project's node-local secrets never reach an intake run
    comments = [t for k, t in ctx.jira.comments if k == "TESTWP-27"]
    assert len(comments) == 1 and "Original description:" in comments[0] and "make a portfolio page pls" in comments[0]
    assert ctx.jira.descriptions == [("TESTWP-27", REWRITE)]
    assert ("add", "TESTWP-27", "aiflow-ready") in ctx.jira.labels
    assert ctx.jira.status_of["TESTWP-27"] == "To Do"
    actions = [a["action"] for a in ctx.db.all("SELECT action FROM write_audit WHERE ticket='TESTWP-27' ORDER BY id")]
    assert actions == ["token", "comment", "description", "fields", "transition"]  # original saved before the rewrite
    assert ctx.db.one("SELECT outcome FROM runs WHERE ticket_key='TESTWP-27'")["outcome"] == "refined"


def test_oversized_intake_is_split_into_ordered_children_before_any_child_is_offered(
        iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-36", status="Intake", labels=("size-s",))
    ctx.db.run("UPDATE tickets SET summary='Implement a complete profile editor', description='large request' WHERE key=%s",
               ("TESTWP-36",))
    plan = [
        {"summary": "Add profile data model", "description": "Business requirement: Store profile data.\n"
         "Acceptance criteria:\n- AC1: Profile data is stored.\nTechnical notes: Model changes.\n"
         "Definition of done: Tests cover AC1."},
        {"summary": "Add profile editor screen", "description": "Business requirement: Edit a profile.\n"
         "Acceptance criteria:\n- AC1: A profile can be edited.\nTechnical notes: Screen changes.\n"
         "Definition of done: Tests cover AC1."},
    ]
    answer = "SPLIT PLAN:\n<<<\n" + json.dumps(plan) + "\n>>>\nINTAKE: SPLIT\n"
    result, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-36", answer)

    children = result["details"]["split_children"]
    assert result["outcome"] == "split" and len(children) == 2
    assert "90" in adapter.prompts[0] and "over 30 minutes" in adapter.prompts[0]
    assert ctx.db.one("SELECT status,labels FROM tickets WHERE key='TESTWP-36'") == {
        "status": "Backlog", "labels": ["size-s", "aiflow-split-parent"]}
    assert [ctx.db.one("SELECT status,blocked_by FROM tickets WHERE key=%s", (key,))
            for key in children] == [{"status": "To Do", "blocked_by": []},
                                     {"status": "To Do", "blocked_by": [children[0]]}]
    assert ("TESTWP-36", children[0], "Relates") in ctx.jira.issue_links
    assert ("TESTWP-36", children[1], "Relates") in ctx.jira.issue_links
    assert (children[0], children[1], "Blocks") in ctx.jira.issue_links
    created = [row for row in ctx.jira.created if row["key"] in children]
    assert all("aiflow-ready" not in row["labels"] for row in created)
    first_offer = next(index for index, event in enumerate(ctx.jira.write_events)
                       if event[0] == "add_label" and event[1] in children and event[2] == "aiflow-ready")
    links_before_offer = {(event[1], event[2], event[3]) for event in ctx.jira.write_events[:first_offer]
                          if event[0] == "link"}
    assert links_before_offer == {("TESTWP-36", children[0], "Relates"),
                                  ("TESTWP-36", children[1], "Relates"),
                                  (children[0], children[1], "Blocks")}
    assert (children[0], "dev") in candidates(iclient, "claude-01")
    assert (children[1], "dev") not in candidates(iclient, "claude-01")
    assert ("TESTWP-36", "dev") not in candidates(iclient, "claude-01")
    dev_run = claim(iclient, children[0], account="claude-01", step="dev")
    again = iclient.post("/v1/jira/TESTWP-36/split", json={"run_id": dev_run["run_id"],
                                                           "fence": dev_run["fence"], "tickets": plan})
    assert again.status_code == 403  # a developer run cannot split intake work


def test_repeating_an_intake_split_resumes_the_same_ordered_children(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-38", status="Intake", labels=("size-s",))
    run = claim(iclient, "TESTWP-38")
    plan = [{"summary": "First child", "description": "AC1: first result"},
            {"summary": "Second child", "description": "AC1: second result"}]
    body = {"run_id": run["run_id"], "fence": run["fence"], "tickets": plan}

    first = iclient.post("/v1/jira/TESTWP-38/split", json=body)
    repeated = iclient.post("/v1/jira/TESTWP-38/split", json=body)

    assert first.status_code == 200 and repeated.status_code == 200
    assert repeated.json()["already_split"] is True
    assert repeated.json()["children"] == first.json()["children"]
    assert len(ctx.jira.created) == 2


def test_oversized_child_is_rejected_before_any_split_child_is_created_or_offered(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-39", status="Intake", labels=("size-s",))
    run = claim(iclient, "TESTWP-39")
    too_large = "AC1: works\n" + ("Detailed implementation requirement. " * 30)
    plan = [{"summary": "Small first part", "description": "AC1: first part works"},
            {"summary": "Oversized second part", "description": too_large}]

    response = iclient.post("/v1/jira/TESTWP-39/split", json={"run_id": run["run_id"],
                              "fence": run["fence"], "tickets": plan})

    assert response.status_code == 422 and "above the 30-minute target" in response.text
    assert not getattr(ctx.jira, "created", [])
    assert candidates(iclient, "claude-01") == []
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-39'")["status"] == "Intake"


def test_split_children_keep_parent_routing_and_review_labels(iclient, ctx):
    labels = ("size-s", "aiflow-ready", "ai-opencode", "tier-senior", "security", "area-flow", "customer-label")
    add_ticket(ctx.db, "TESTWP-40", status="Intake", labels=labels)
    run = claim(iclient, "TESTWP-40")
    plan = [{"summary": "First secured part", "description": "AC1: first part works"},
            {"summary": "Second secured part", "description": "AC1: second part works"}]

    response = iclient.post("/v1/jira/TESTWP-40/split", json={"run_id": run["run_id"],
                              "fence": run["fence"], "tickets": plan})

    assert response.status_code == 200, response.text
    children = response.json()["children"]
    for key in children:
        stored = ctx.db.one("SELECT labels FROM tickets WHERE key=%s", (key,))["labels"]
        assert set(labels) - {"size-s", "aiflow-ready"} <= set(stored)
        assert "size-s" not in stored and "aiflow-split-parent" not in stored
        assert "aiflow-ready" in stored
        jira_child = next(row for row in ctx.jira.created if row["key"] == key)
        assert set(labels) - {"size-s", "aiflow-ready"} <= set(jira_child["labels"])
    assert all((key, "dev") in candidates(iclient, "claude-01") for key in [children[0]])
    assert all((key, "dev") not in candidates(iclient, "opencode-01") for key in children)


@pytest.mark.parametrize("plan", [[], [{}], [{"summary": 5, "description": "AC1: yes"},
                                               {"summary": "second", "description": "AC1: yes"}],
                                   [{"summary": "first", "description": "missing AC"},
                                    {"summary": "second", "description": "AC1: yes"}]])
def test_split_plan_rejects_empty_wrong_type_and_missing_acceptance_criteria(plan):
    from aiflow.v4.core.ticket_splitting import InvalidSplitPlan, validate_plan

    with pytest.raises(InvalidSplitPlan):
        validate_plan(plan)


def test_split_plan_enforces_count_and_summary_boundaries():
    from aiflow.v4.core.ticket_splitting import InvalidSplitPlan, MAX_CHILDREN, validate_plan

    child = {"summary": "Child task", "description": "AC1: testable result"}
    assert len(validate_plan([child, child])) == 2
    assert len(validate_plan([child] * MAX_CHILDREN)) == MAX_CHILDREN
    assert len(validate_plan([{"summary": "12345", "description": "AC1: " + "x" * 9_995}, child])) == 2
    assert len(validate_plan([{"summary": "x" * 240, "description": "AC1: max summary"}, child])) == 2
    with pytest.raises(InvalidSplitPlan, match="between 2"):
        validate_plan([child] * (MAX_CHILDREN + 1))
    with pytest.raises(InvalidSplitPlan, match="summary"):
        validate_plan([{"summary": "", "description": "AC1: x"}, child])
    with pytest.raises(InvalidSplitPlan, match="summary"):
        validate_plan([{"summary": "x" * 241, "description": "AC1: too long"}, child])
    with pytest.raises(InvalidSplitPlan, match="description"):
        validate_plan([{"summary": "valid child", "description": "AC1: " + "x" * 9_996}, child])
    with pytest.raises(InvalidSplitPlan, match="description"):
        validate_plan([{"summary": "valid child", "description": ""}, child])
    with pytest.raises(InvalidSplitPlan, match="between 2"):
        validate_plan("not a ticket list")


def test_split_endpoint_requires_intake_run_and_an_over_target_estimate(iclient, ctx):
    add_ticket(ctx.db, "TESTWP-37", status="Intake", labels=("size-xs",))
    run = claim(iclient, "TESTWP-37")
    plan = [{"summary": "First part", "description": "AC1: first result"},
            {"summary": "Second part", "description": "AC1: second result"}]
    response = iclient.post("/v1/jira/TESTWP-37/split", json={"run_id": run["run_id"],
                              "fence": run["fence"], "tickets": plan})
    assert response.status_code == 422 and "within the 30-minute target" in response.text
    assert not getattr(ctx.jira, "created", [])


def test_typed_intake_uses_bug_template_and_preserves_known_and_unknown_labels(iclient, ctx, tmp_path, monkeypatch):
    labels = (
        "aiflow-ready", "rework", "require-human", "tier-senior", "ai-grok", "area-flow",
        "regression", "uat-bug", "blocked-external", "needs-design", "breaking-change", "perf", "a11y", "seo",
        "security", "customer-custom",
    )
    add_ticket(ctx.db, "TESTWP-34", status="Intake", labels=labels)
    ctx.db.run("UPDATE tickets SET type='Bug' WHERE key='TESTWP-34'")
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-34",
                              f"DESCRIPTION:\n<<<\n{BUG_REWRITE}\n>>>\nINTAKE: REWRITE\n")

    assert res["outcome"] == "refined"
    prompt = adapter.prompts[0]
    assert "Jira issue type: Bug. Use the Bug template." in prompt
    for section in ("Steps to reproduce:", "Expected result:", "Actual result:", "Environment:"):
        assert section in prompt
    assert "ai-grok (recognized)" in prompt and "area-flow (recognized)" in prompt
    assert "customer-custom (unrecognized; preserve)" in prompt
    assert set(labels) | {"aiflow-ready"} == set(ctx.db.one("SELECT labels FROM tickets WHERE key='TESTWP-34'")["labels"])
    assert ctx.jira.labels == [("add", "TESTWP-34", "aiflow-ready")]


@pytest.mark.parametrize("issue_type,extras", EXPECTED_EXTRA_SECTIONS.items())
def test_each_issue_type_requires_its_documented_template_sections(issue_type, extras):
    common_and_extras = ("Business requirement:", *extras, "Acceptance criteria:", "Technical notes:",
                         "Definition of done:")
    assert intake.required_sections(issue_type) == common_and_extras
    description = "\n".join(
        f"{section}\n- AC1: observable behavior" if section == "Acceptance criteria:" else f"{section} example"
        for section in common_and_extras
    )
    wrapped = f"DESCRIPTION:\n<<<\n{description}\n>>>\nINTAKE: REWRITE"
    assert intake.parse(wrapped, issue_type) == ("rewrite", description)
    if extras:
        missing = description.replace(f"{extras[0]} example\n", "")
        kind, why = intake.parse(f"DESCRIPTION:\n<<<\n{missing}\n>>>\nINTAKE: REWRITE", issue_type)
        assert kind == "invalid" and extras[0] in why


@pytest.mark.parametrize("issue_type", [None, "", "Task", "Custom type"])
def test_unknown_and_missing_issue_types_use_story_template(issue_type):
    assert intake.effective_issue_type(issue_type) == "Story"
    assert intake.required_sections(issue_type) == intake.required_sections("Story")
    assert intake.parse(f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE", issue_type)[0] == "rewrite"


def test_jira_mirror_keeps_default_task_type_when_issue_type_is_missing(ctx):
    mirrored = ctx.jira.normalize({"key": "TESTWP-35", "fields": {}})
    assert mirrored["type"] == "Task"
    assert intake.effective_issue_type(mirrored["type"]) == "Story"


def test_intake_recognizes_all_label_names_and_patterns():
    recognized = (
        "aiflow-ready", "aiflow-hold", "rework", "require-human", "tier-senior", "ai-grok", "area-core",
        "area-theme", "area-plugin", "area-platform", "area-flow", "regression", "uat-bug", "blocked-external",
        "needs-design", "breaking-change", "perf", "a11y", "seo", "security",
    )
    assert all(intake.is_known_label(label) for label in recognized)
    assert not intake.is_known_label("area-other")
    assert not intake.is_known_label("customer-custom")


def test_issue_type_document_covers_templates_labels_and_jira_setup_plan():
    doc = (intake.REPO_ROOT / "docs" / "v4" / "issue-types.md").read_text()
    for issue_type in EXPECTED_EXTRA_SECTIONS:
        assert f"| {issue_type} |" in doc
    for section in ("Business requirement", "Acceptance criteria", "Technical notes", "Definition of done",
                    "Steps to reproduce", "Expected result", "Actual result", "Environment", "Question", "Timebox",
                    "Output", "Problem", "Risk", "Threat", "Impact", "Reader"):
        assert section in doc
    for label in ("aiflow-ready", "aiflow-hold", "rework", "require-human", "tier-senior", "ai-", "area-", "regression",
                  "uat-bug", "blocked-external", "needs-design", "breaking-change", "perf", "a11y", "seo", "security"):
        assert label in doc
    assert "TESTFLOW-50" in doc and "owner" in doc and "Task" in doc


def test_questions_move_the_ticket_to_needs_owner(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-28", status="Intake", labels=())
    answer = "QUESTIONS:\n- Which pages list the projects?\n- Is a mobile layout needed?\nINTAKE: QUESTIONS\n"
    res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-28", answer, spec=None)

    assert res["outcome"] == "questions"
    assert "has no docs/SPEC.md" in adapter.prompts[0]
    comments = [t for k, t in ctx.jira.comments if k == "TESTWP-28"]
    assert len(comments) == 1 and "Which pages list the projects?" in comments[0] and "mobile layout" in comments[0]
    assert ctx.jira.status_of["TESTWP-28"] == "Needs owner"
    assert not getattr(ctx.jira, "descriptions", [])
    assert ("add", "TESTWP-28", "aiflow-ready") not in ctx.jira.labels


def test_owner_answers_in_comments_reach_the_next_intake(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-29", status="Intake", labels=())
    ctx.jira.recent_comment_data["TESTWP-29"] = [
        {"created": "2026-10-21T12:20:00Z", "author": "Owner", "body": "Only the projects page; mobile layout yes."}]
    answer = "QUESTIONS:\n- Anything else?\nINTAKE: QUESTIONS\n"
    _res, adapter = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-29", answer)
    assert "Only the projects page; mobile layout yes." in adapter.prompts[0]


def test_unusable_answer_leaves_the_ticket_in_intake(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-29", status="Intake", labels=())
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-29", "Sure, here is a rewrite: ...")
    assert res["outcome"] == "failed"
    assert "TESTWP-29" not in ctx.jira.status_of and not ctx.jira.comments


def test_malformed_rewrite_is_rejected(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-31", status="Intake", labels=())
    answer = "DESCRIPTION:\n<<<\nMake a portfolio page.\nAcceptance criteria:\n- it works\n>>>\nINTAKE: REWRITE\n"
    res, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-31", answer)
    assert res["outcome"] == "failed" and "does not follow the template" in res["summary"]
    assert "Business requirement:" in res["summary"] and "AC1" in res["summary"]
    assert not getattr(ctx.jira, "descriptions", [])
    comments = [text for key, text in ctx.jira.comments if key == "TESTWP-31"]
    assert len(comments) == 1 and "does not follow the template" in comments[0]
    assert "ticket remains in Intake" in comments[0]
    assert "TESTWP-31" not in ctx.jira.status_of and ("add", "TESTWP-31", "aiflow-ready") not in ctx.jira.labels


def test_server_question_from_intake_is_answered_by_environment_without_owner_transition(iclient, ctx, tmp_path, monkeypatch):
    add_ticket(ctx.db, "TESTWP-36", status="Intake", labels=())
    answer = "QUESTIONS:\n- Which address should I use?\nINTAKE: QUESTIONS\n"
    result, _ = run_worker(iclient, ctx, tmp_path, monkeypatch, "TESTWP-36", answer)

    assert result["outcome"] == "environment_question_answered"
    comments = [text for key, text in ctx.jira.comments if key == "TESTWP-36"]
    assert len(comments) == 1 and "MISSING-ENV" in comments[0]
    assert "Which address should I use?" not in comments[0]
    assert ctx.jira.status_of.get("TESTWP-36", "Intake") == "Intake"


def test_parse():
    assert intake.parse(f"DESCRIPTION:\n<<<\n{REWRITE}\n>>>\nINTAKE: REWRITE") == ("rewrite", REWRITE)
    assert intake.parse("QUESTIONS:\n- why?\nINTAKE: QUESTIONS") == ("questions", "- why?")
    assert intake.parse("DESCRIPTION:\n<<<\n\n>>>\nINTAKE: REWRITE") == ("", "")
    assert intake.parse("QUESTIONS:\n- why?\nINTAKE: REWRITE") == ("", "")


@pytest.mark.parametrize("broken, lacks", [
    (REWRITE.replace("Technical notes: WordPress theme template.\n", ""), "Technical notes:"),
    (REWRITE.replace("Business requirement:", "Goal:"), "Business requirement:"),
    (REWRITE.replace("- AC1: /portfolio lists the projects", "- /portfolio lists the projects"), "AC1"),
    (REWRITE.replace("Definition of done:\n- tests cover AC1", ""), "Definition of done:"),
    ("Definition of done:\n- x\n" + REWRITE.replace("Definition of done:\n- tests cover AC1", ""), "Definition of done:"),
    ("just some text", "Acceptance criteria:"),
])
def test_parse_rejects_rewrites_that_miss_the_template(broken, lacks):
    kind, why = intake.parse(f"DESCRIPTION:\n<<<\n{broken}\n>>>\nINTAKE: REWRITE")
    assert kind == "invalid" and lacks in why
