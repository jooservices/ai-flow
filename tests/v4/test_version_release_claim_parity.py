from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from aiflow.v4.control import api, gateway_helpers
from aiflow.v4.control.features.version_release import claim_steps, routes

def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=lambda item: getattr(item, "obj", str(item)))

class RecordingDb:
    def __init__(self, scenario, calls):
        self.s, self.calls = scenario, calls

    def one(self, query, _params=()):
        if "SELECT projects,dynamic" in query:
            self.calls.append("db:node")
            return {"projects": ["TESTWP"], "dynamic": False, "accepting_work": self.s.get("accepting", True),
                    "readiness": {}}
        if "SELECT tools FROM nodes" in query:
            self.calls.append("db:tools")
            return {"tools": {}} if self.s.get("tools_missing") else {"tools": {"container": "ok"}}
        if "SELECT status FROM version_releases" in query:
            self.calls.append("db:release-state")
            status = self.s.get("release_status")
            return {"status": status} if status else None
        raise AssertionError(f"unexpected query: {query}")

    def run(self, query, params=()):
        tag = "run-details" if "UPDATE runs SET details" in query else "release-row" if "INSERT INTO version_releases" in query else "account-working"
        self.calls.append(f"db:{tag}:{_json(params)}")

class LeaseRegistry:
    def __init__(self, scenario, calls):
        self.s, self.calls = scenario, calls

    def cooling(self, _account):
        self.calls.append("lease:cooling")
        return self.s.get("cooling", False)

    def claim(self, key, run_id, lease_ms):
        self.calls.append(f"lease:claim:{key}:{run_id}:{lease_ms}")
        return SimpleNamespace(ok=self.s.get("lease_ok", True), fence=19)

    def release(self, key, run_id, fence):
        self.calls.append(f"lease:release:{key}:{run_id}:{fence}")

class RecordingGithub:
    def __init__(self, scenario, calls):
        self.s, self.calls = scenario, calls

    def branch_exists(self, _repo, branch):
        self.calls.append(f"github:exists:{branch}")
        return self.s.get("branch_exists", False)

    def create_branch(self, _repo, branch, source):
        self.calls.append(f"github:create:{branch}:{source}")

    def create_branch_at_sha(self, _repo, branch, sha):
        self.calls.append(f"github:create-at:{branch}:{sha}")

    def branch_sha(self, _repo, branch):
        self.calls.append(f"github:sha:{branch}")
        return "source-sha" if branch == "develop" else "release-head"

class RecordingEvents:
    def __init__(self, calls):
        self.calls = calls

    def emit(self, name, **fields):
        self.calls.append(f"event:{name}:{_json(fields)}")

class FakeApp:
    def get(self, *_args, **_kwargs):
        return lambda function: function

    post = get

@dataclass
class FakeContext:
    db: object
    leases: object
    events: object
    settings: object
    github: object = None
    repo_value: object = None

    def repo(self):
        return self.repo_value

def _observe(monkeypatch, scenario):
    calls = []
    project = SimpleNamespace(key="TESTWP", repo=scenario.get("repo", "owner/project"), repo_name="project",
                              base="develop", stack="php", version_release={"enabled": scenario.get("enabled", True)},
                              environment_items=[], settings_for_repo=lambda _repo: {"containers": scenario.get("containers", False)},
                              work_minutes=lambda: 10)
    selected = SimpleNamespace(**vars(project))
    selected.repo = scenario.get("selected_repo", project.repo)
    selected.version_release = {"enabled": scenario.get("selected_enabled", True)}
    account = SimpleNamespace(id=scenario.get("account_id", "release-1"), provider="claude", roles=scenario.get("roles", ["release"]),
                              runs_on=lambda _node: scenario.get("runs_on", True))
    repo = SimpleNamespace(projects={"TESTWP": project} if scenario.get("project_exists", True) else {}, versions={})
    repo.account = lambda _id: account if scenario.get("account_exists", True) else None
    repo.projects_for_node = lambda *_args: ["TESTWP"] if scenario.get("assigned", True) else []
    repo.accounts_on = lambda _node: [account]
    ctx = FakeContext(RecordingDb(scenario, calls), LeaseRegistry(scenario, calls), RecordingEvents(calls),
                      SimpleNamespace(license_public_key="test-license-key", lease_ms=60000))
    ctx.repo_value = repo
    gateway = RecordingGithub(scenario, calls)
    lookups = [0]

    def get_gateway(*_args):
        lookups[0] += 1
        calls.append("github:lookup")
        if scenario.get("github_missing") or (scenario.get("github_missing_after") and lookups[0] == 2):
            return None
        return gateway

    monkeypatch.setattr(gateway_helpers, "github_gateway", get_gateway)
    monkeypatch.setattr(api.log, "warning", lambda *args: calls.append("log:warning"))
    monkeypatch.setattr(routes.load_policy_api, "version_claim_capacity", lambda *_args: calls.append("policy:capacity") or 6)
    monkeypatch.setattr(routes.load_policy_api, "claim_run", lambda *_args: calls.append("policy:claim-run") or scenario.get("claim_run", True))
    monkeypatch.setattr(routes.licensing, "enforce_work_entitlement", lambda *_args, **_kw: calls.append("license:work") or _license(scenario.get("work_license")))
    monkeypatch.setattr(routes.licensing, "enforce_project_entitlement", lambda *_args, **_kw: calls.append("license:project") or _license(scenario.get("project_license")))
    monkeypatch.setattr(routes.rollout, "node_compatible", lambda *_args: calls.append("policy:node") or {"compatible": scenario.get("compatible", True), "reason": "old node"})
    monkeypatch.setattr(routes.shared_accounts, "may_take", lambda *_args: (calls.append("account:may-take") or (scenario.get("may_take", True), scenario.get("may_take_reason", "no capacity"))))
    monkeypatch.setattr(routes.system_check_api, "require_project_ready", lambda *_args: calls.append("system:ready") or _raise(scenario.get("readiness_error")))
    monkeypatch.setattr(routes, "toolchain_gaps", lambda *_args: scenario.get("tools_missing", False))
    _patch_release(monkeypatch, scenario, calls, project, selected)
    deps = SimpleNamespace(account_master_paused=lambda _account: scenario.get("paused", False), bot_identity=lambda: "bot",
                           fenced=lambda *_args: None, node_auth=lambda: "node", quota_peer_release_candidates=lambda _account: {("TESTWP", "101", project.repo)} if scenario.get("peer") else set())
    handler = routes.install(FakeApp(), ctx, deps).version_release_claim
    body = routes.VersionReleaseClaimBody(project="TESTWP", version_id="101", account="release-1",
                                          repository=scenario.get("repository", ""))
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: UUID("00000000-0000-0000-0000-000000000001"))
    monkeypatch.setattr(routes.secrets, "token_urlsafe", lambda _length: "stable-run-token")
    monkeypatch.setattr(routes, "github_connection_used", SimpleNamespace(get=lambda: "connection-1"))
    monkeypatch.setattr(routes, "DEFAULT_ID", "default-connection")
    try:
        result = handler(body, "node-1")
        observed = {"result": result}
    except HTTPException as exc:
        observed = {"result": {"http": {"status": exc.status_code, "detail": exc.detail}}}
    observed["calls"] = calls
    return observed

def _license(message):
    if message:
        raise routes.licensing.LicenseError(message)
    return {"ok": True}

def _raise(message):
    if message:
        raise HTTPException(409, message)

def _patch_release(monkeypatch, scenario, calls, project, selected):
    release = routes.version_release
    version = {"id": "101", "name": "Core 1.0", "repository": "", "repositories": [project.repo]}
    snapshot = {"ready": scenario.get("snapshot_ready", True), "tickets": [{"key": "TESTWP-1"}], "reasons": ["gate"]}
    if scenario.get("resume"):
        snapshot.update(resume_after_merge=True, source_sha="resumed-source", release_branch="release/resume", release_head_sha="resume-head")
    monkeypatch.setattr(release, "enabled", lambda item: item.version_release.get("enabled", False))
    monkeypatch.setattr(release, "project_versions", lambda *_args: [version] if scenario.get("version_exists", True) else [])
    monkeypatch.setattr(release, "project_for_version", lambda *_args: selected)
    monkeypatch.setattr(release, "post_merge_resume", lambda *_args: calls.append("release:resume") or (snapshot if scenario.get("resume") else None))
    monkeypatch.setattr(release, "evaluate", lambda *_args: calls.append("release:evaluate") or snapshot)
    monkeypatch.setattr(release, "record_gate", lambda *_args: calls.append("release:record-gate"))
    monkeypatch.setattr(release, "version_identity", lambda key, version_id: f"{key}-REL-{version_id}")
    monkeypatch.setattr(release, "source_branch", lambda _project: "develop")
    monkeypatch.setattr(release, "release_branch", lambda *_args: "release/core-1.0")
    monkeypatch.setattr(release, "tracker_for", lambda *_args: "TESTWP-REL")
    monkeypatch.setattr(release, "check_commands", lambda _project: ["check release"])
    monkeypatch.setattr(release, "_audit", lambda *_args: calls.append("audit:github-release-branch"))
    monkeypatch.setattr(routes.knowledge, "context_pack", lambda *_args, **_kw: _context(scenario, calls))

def _context(scenario, calls):
    calls.append("knowledge:context-pack")
    if scenario.get("context_error"):
        raise RuntimeError("fixture search failure")
    return {"project": "TESTWP", "ticket": "TESTWP-REL-101", "items": []}

def _unit_step_calls():
    calls = []
    scenario = {}
    project = SimpleNamespace(key="TESTWP", repo="owner/project", repo_name="project",
                              version_release={"enabled": True}, settings_for_repo=lambda _repo: {})
    account = SimpleNamespace(id="release-1", roles=["release"], runs_on=lambda _node: True)
    repo = SimpleNamespace(projects={"TESTWP": project}, versions={})
    repo.account = lambda _account: account
    repo.projects_for_node = lambda *_args: ["TESTWP"]
    repo.accounts_on = lambda _node: [account]
    ctx = FakeContext(RecordingDb(scenario, calls), LeaseRegistry(scenario, calls), RecordingEvents(calls),
                      SimpleNamespace(license_public_key="key", lease_ms=1), repo_value=repo)
    body = SimpleNamespace(account="release-1", project="TESTWP", version_id="101", repository="")
    version = {"id": "101", "name": "Core 1.0", "repositories": [project.repo]}
    snapshot = {"ready": True, "tickets": [], "reasons": []}
    github = SimpleNamespace(branch_exists=lambda *_args: False, branch_sha=lambda *_args: "sha",
                             create_branch=lambda *_args: None, create_branch_at_sha=lambda *_args: None)
    no_op = lambda *_args, **_kwargs: None
    return {
        "check_claim_policy": lambda: claim_steps.check_claim_policy(
            ctx, body, "node", lambda *_: 6, lambda *_a, **_k: {}, Exception,
            lambda *_: {"compatible": True}, lambda: None, HTTPException),
        "check_claim_owner": lambda: claim_steps.check_claim_owner(
            ctx, body, "node", {}, lambda *_: False, no_op, Exception, lambda *_: (True, ""),
            lambda _item: True, HTTPException),
        "check_claim_node": lambda: claim_steps.check_claim_node(ctx, repo, project, body, "node", no_op, HTTPException),
        "resolve_claim_version": lambda: claim_steps.resolve_claim_version(
            ctx, body, project, lambda *_: [version], lambda *_: project, lambda _item: True, HTTPException),
        "check_claim_runtime": lambda: claim_steps.check_claim_runtime(
            ctx, repo, project, version, "node", body.account, lambda *_: False, "container", lambda *_: set(), HTTPException),
        "evaluate_claim_gate": lambda: claim_steps.evaluate_claim_gate(
            ctx, project, version, lambda *_: github, lambda context, **_kw: context,
            lambda *_: snapshot, no_op, no_op, HTTPException),
        "create_claim_branch": lambda: claim_steps.create_claim_branch(
            project, version, snapshot, github, lambda _item: "develop", lambda *_: "release/core"),
        "persist_claim_rows": lambda: claim_steps.persist_claim_rows(
            ctx, project, version, "run-1", "TESTWP-REL", {}, "connection", snapshot, lambda value: value),
    }

@pytest.mark.parametrize("step", ["check_claim_policy", "check_claim_owner", "check_claim_node",
                                   "resolve_claim_version", "check_claim_runtime", "evaluate_claim_gate",
                                   "create_claim_branch", "persist_claim_rows"])
def test_claim_steps_run_with_unit_fakes(step):
    _unit_step_calls()[step]()


FIXTURE = Path(__file__).parent / "fixtures" / "version_release_claim_parity.jsonl"
RECORDED = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
CASES = [item["case"] for item in RECORDED]


def test_fixture_covers_claim_case_table():
    assert len(RECORDED) >= 20 and all({"case", "result", "calls"} <= item.keys() for item in RECORDED)


@pytest.mark.parametrize("case", CASES, ids=lambda item: item["name"])
def test_claim_matches_recorded_current_behavior(monkeypatch, case):
    actual = _observe(monkeypatch, case)
    expected = next(item for item in RECORDED if item["name"] == case["name"])
    assert actual == {"result": expected["result"], "calls": expected["calls"]}
