"""Fewer merge conflicts (JAIF-253): hot-file scheduling (AC1), automatic branch update (AC2), one file per topic
(AC3), the merge queue (AC4) and the conflict share in Command Center (AC5)."""
from __future__ import annotations

import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import conflict_loss, hotfiles, merge_flow, merge_queue
from aiflow.v4.control.db import J
from aiflow.v4.core import topics
from aiflow.v4.worker import verify
from aiflow.v4.worker.gitops import Git
from test_repositories import _API, _adapter, _claim

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = "aiflow/v4/control/ui/console.html"


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def describe(db, key, summary, description=""):
    db.run("UPDATE tickets SET summary=%s, description=%s WHERE key=%s", (summary, description, key))


# --- AC1: hot-file scheduling --------------------------------------------------------------------------------------

def test_prediction_reads_paths_areas_epic_and_history(db):
    add_ticket(db, "TESTWP-1", status="To Do")
    db.run("UPDATE tickets SET type='Epic', summary='Console work', description='all in the Command Center' WHERE key='TESTWP-1'")
    add_ticket(db, "TESTWP-2")
    db.run("UPDATE tickets SET parent_key='TESTWP-1' WHERE key='TESTWP-2'")
    add_ticket(db, "TESTWP-3")
    db.run("UPDATE tickets SET parent_key='TESTWP-1', summary='Pages', description='edit tests/v4/conftest.py' WHERE key='TESTWP-3'")
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status, outcome, details)"
           " VALUES (%s,'TESTWP-3','TESTWP','dev','claude-01','claude','cc',1,'finished','done',%s)",
           (str(uuid.uuid4()), '{"changed_files": ["pyproject.toml", "docs/v4/x.md"]}'))
    ticket = db.one("SELECT * FROM tickets WHERE key='TESTWP-2'")
    ticket["description"] = "Change aiflow/v4/control/ui/console.html and aiflow/v4/worker/dev.py"
    footprint = hotfiles.predict(db, ticket, "TESTWP-1")
    assert CONSOLE in footprint["hot"] and "aiflow/v4/worker/dev.py" in footprint["files"]
    assert "aiflow/v4/worker/dev.py" not in footprint["hot"]  # not a hot file
    assert footprint["sources"][CONSOLE] == "ticket"
    # a ticket that names no path still gets the hot file of its epic's area and of the epic's earlier runs
    quiet = {**ticket, "summary": "Polish", "description": "small things"}
    quiet_print = hotfiles.predict(db, quiet, "TESTWP-1")
    assert CONSOLE in quiet_print["hot"] and quiet_print["sources"][CONSOLE] == "epic"
    assert "pyproject.toml" in quiet_print["hot"] and quiet_print["sources"]["pyproject.toml"] == "history"
    assert "Command Center console page" in quiet_print["areas"]


def test_keyword_area_points_to_the_console_page(db):
    footprint = hotfiles.predict(db, {"key": "TESTWP-9", "summary": "New dashboard card", "description": ""})
    assert footprint["hot"] == [CONSOLE, "aiflow/v4/control/ui/licensing_notice.js"]


def test_hot_scheduling_covers_develop_giants_node_agent_and_shared_tests():
    develop_baseline_giants = (
        "aiflow/v4/control/api.py",
        "aiflow/v4/control/ui/console.html",
        "aiflow/supervisor.py",
        "tests/v4/test_console.py",
        "aiflow/v4/worker/containers.py",
        "tests/test_supervisor.py",
    )
    shared_test_paths = (
        "tests/v4/conftest.py",
        "tests/v4/fixtures/grok-streaming-json.jsonl",
        "tests/v4/test_api.py",
        "tests/v4/console_browser_suite.cjs",
        "tests/v4/log_viewer_browser_suite.cjs",
    )

    for path in (*develop_baseline_giants, "aiflow/v4/node/agent.py", *shared_test_paths):
        assert hotfiles.is_hot(path), f"{path} is missing from JAIF-253 hot-file scheduling"
    assert hotfiles.paths_in("Update tests/v4/fixtures/grok-streaming-json.jsonl") == [
        "tests/v4/fixtures/grok-streaming-json.jsonl"
    ]


def test_blocking_needs_a_shared_hot_file():
    mine = {"hot": ["a.py", "b.py"]}
    assert hotfiles.blocking_ticket(mine, {"X-1": ["c.py"], "X-2": ["b.py", "z"]}) == ("X-2", ["b.py"])
    assert hotfiles.blocking_ticket(mine, {"X-1": ["c.py"]}) is None
    assert hotfiles.blocking_ticket({"hot": []}, {"X-1": ["c.py"]}) is None


def test_second_ticket_on_a_hot_file_waits_and_another_ticket_is_offered(client, ctx):
    cc = join(client, "cc")
    for key in ("TESTWP-1", "TESTWP-2", "TESTWP-3"):
        add_ticket(ctx.db, key)
    describe(ctx.db, "TESTWP-1", "Add a card", "Edit " + CONSOLE)
    describe(ctx.db, "TESTWP-2", "Another card", "Also edit " + CONSOLE)
    describe(ctx.db, "TESTWP-3", "Backend rule", "Edit aiflow/v4/worker/loop.py")
    ctx.db.run("UPDATE tickets SET priority='High' WHERE key='TESTWP-1'")

    def offered():
        return [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                             headers=cc).json()["tickets"]]

    assert offered() == ["TESTWP-1", "TESTWP-3", "TESTWP-2"] or offered() == ["TESTWP-1", "TESTWP-2", "TESTWP-3"]
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).json()
    assert claim["claimed"]
    # TESTWP-2 expects to touch the same hot file as the running TESTWP-1: it waits, TESTWP-3 is offered instead
    refused = client.post("/v1/work/claim", json={"ticket": "TESTWP-2", "account": "codex-01"}, headers=cc)
    assert refused.status_code in (403, 409)
    ctx.db.run("UPDATE runs SET status='finished', outcome='done' WHERE id=%s", (claim["run_id"],))
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-1'")
    assert "TESTWP-2" not in offered() and "TESTWP-3" in offered()  # an open pull request holds the file as well
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-1'")
    assert "TESTWP-2" in offered()  # merged: the file is free


def test_waiting_ticket_stays_blocked_after_six_hours_until_holder_releases_hot_file(client, ctx):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-1", status="In Verify")
    add_ticket(ctx.db, "TESTWP-2")
    add_ticket(ctx.db, "TESTWP-3")
    describe(ctx.db, "TESTWP-1", "Card", "Edit " + CONSOLE)
    describe(ctx.db, "TESTWP-2", "Card two", "Edit " + CONSOLE)
    describe(ctx.db, "TESTWP-3", "Backend rule", "Edit aiflow/v4/worker/loop.py")
    # an old backlog ticket is still held on its first check: the wait counts from the first block, not its age
    ctx.db.run("UPDATE tickets SET jira_created=now() - interval '3 days', jira_updated=now() - interval '3 days'"
               " WHERE key='TESTWP-2'")
    keys = lambda: [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                                 headers=cc).json()["tickets"]]
    assert "TESTWP-2" not in keys()
    first = ctx.db.one("SELECT blocked_at FROM hot_file_blocked WHERE ticket_key='TESTWP-2'")["blocked_at"]
    assert "TESTWP-2" not in keys()
    assert ctx.db.one("SELECT blocked_at FROM hot_file_blocked WHERE ticket_key='TESTWP-2'")["blocked_at"] == first
    ctx.db.run("UPDATE hot_file_blocked SET blocked_at=now() - interval '7 hours' WHERE ticket_key='TESTWP-2'")
    assert "TESTWP-2" not in keys()  # elapsed time cannot schedule overlapping work
    assert "TESTWP-3" in keys()  # another ticket remains available
    # once nothing blocks it, the record goes and the waiting ticket is offered
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-1'")
    assert "TESTWP-2" in keys()
    assert ctx.db.one("SELECT 1 AS x FROM hot_file_blocked WHERE ticket_key='TESTWP-2'") is None


def test_simultaneous_claims_reserve_a_hot_file_for_one_ticket_only(db):
    barrier = threading.Barrier(2)
    results = {}

    def go(key):
        barrier.wait(5)
        results[key] = hotfiles.reserve(db, key, f"run-{key}", [CONSOLE, "pyproject.toml"])

    threads = [threading.Thread(target=go, args=(k,)) for k in ("TESTWP-1", "TESTWP-2")]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    winners = [k for k, v in results.items() if v is None]
    assert len(winners) == 1
    loser = next(k for k in results if k not in winners)
    assert results[loser] == (winners[0], sorted([CONSOLE, "pyproject.toml"]))
    assert hotfiles.reserve(db, loser, "run-x", [CONSOLE]) == (winners[0], [CONSOLE])
    hotfiles.unreserve(db, f"run-{winners[0]}")
    assert hotfiles.reserve(db, loser, "run-x", [CONSOLE]) is None
    hotfiles.unreserve(db, "run-x")
    assert hotfiles.reserve(db, winners[0], "run-y", [CONSOLE]) is None


@pytest.mark.parametrize(("holder_status", "pr_state"), [
    ("Human review", ""), ("Ready to merge", ""), ("In Verify", "open")
])
def test_live_or_open_pr_reservation_cannot_be_replaced_after_six_hours(db, holder_status, pr_state):
    add_ticket(db, "TESTWP-1", status=holder_status)
    add_ticket(db, "TESTWP-2")
    if pr_state:
        db.run("UPDATE tickets SET pr_state=%s WHERE key='TESTWP-1'", (pr_state,))
    assert hotfiles.reserve(db, "TESTWP-1", "finished-dev-run", [CONSOLE]) is None
    db.run("UPDATE hot_file_reservations SET reserved_at=now() - interval '7 hours' WHERE ticket_key='TESTWP-1'")

    assert hotfiles.reserve(db, "TESTWP-2", "second-run", [CONSOLE]) == ("TESTWP-1", [CONSOLE])
    assert db.one("SELECT ticket_key FROM hot_file_reservations WHERE path=%s", (CONSOLE,))["ticket_key"] == "TESTWP-1"

    db.run("UPDATE tickets SET status='Done', pr_state='merged' WHERE key='TESTWP-1'")
    assert hotfiles.reserve(db, "TESTWP-2", "second-run", [CONSOLE]) is None


def test_stale_reservation_is_dropped(db):
    assert hotfiles.reserve(db, "TESTWP-1", "gone", [CONSOLE]) is None
    assert hotfiles.reserve(db, "TESTWP-2", "run-2", [CONSOLE]) == ("TESTWP-1", [CONSOLE])  # fresh: still held
    db.run("UPDATE hot_file_reservations SET reserved_at = now() - interval '1 hour'")  # no running run, no open PR
    assert hotfiles.reserve(db, "TESTWP-2", "run-2", [CONSOLE]) is None


def test_claim_refuses_a_hot_file_reserved_by_a_concurrent_claim(client, ctx):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-1")
    add_ticket(ctx.db, "TESTWP-2")
    describe(ctx.db, "TESTWP-1", "Card", "Edit " + CONSOLE)
    describe(ctx.db, "TESTWP-2", "Card two", "Edit " + CONSOLE)
    # TESTWP-1 reserved the file in a claim that is not visible as a running run yet: eligibility cannot see it
    assert hotfiles.reserve(ctx.db, "TESTWP-1", "run-1", [CONSOLE]) is None
    refused = client.post("/v1/work/claim", json={"ticket": "TESTWP-2", "account": "claude-01"}, headers=cc)
    assert refused.status_code == 409 and "reserved by TESTWP-1" in refused.json()["reason"]
    assert ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key='TESTWP-2'") is None
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).json()
    assert claim["claimed"]
    assert ctx.db.one("SELECT ticket_key FROM hot_file_reservations WHERE path=%s", (CONSOLE,))["ticket_key"] == "TESTWP-1"


# --- AC2: automatic update ------------------------------------------------------------------------------------------

def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin", "HOME": str(cwd)}).stdout


def origin_with_branch(tmp: Path, conflicting: bool) -> Path:
    o = tmp / "origin"
    o.mkdir()
    sh(o, "init", "-q", "-b", "develop")
    (o / "a.txt").write_text("one\n")
    (o / "b.txt").write_text("one\n")
    sh(o, "add", "-A"); sh(o, "commit", "-q", "-m", "init")
    sh(o, "checkout", "-q", "-b", "TESTWP-1-x")
    (o / "a.txt").write_text("branch\n")
    sh(o, "commit", "-q", "-am", "branch change")
    sh(o, "checkout", "-q", "develop")
    (o / ("a.txt" if conflicting else "b.txt")).write_text("develop\n")
    sh(o, "commit", "-q", "-am", "develop change")
    return o


@pytest.mark.parametrize("conflicting", [False, True])
def test_behind_branch_is_clean_or_conflicting(tmp_path, conflicting):
    o = origin_with_branch(tmp_path, conflicting)
    g = Git(str(tmp_path / "srv"))
    mirror = tmp_path / "srv" / "mirrors" / "m.git"
    mirror.parent.mkdir(parents=True)
    g.run("clone", "--bare", "-q", str(o), str(mirror))
    g.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    g.run("fetch", "-q", "origin", cwd=mirror)
    assert g.behind(mirror, "develop", "origin/TESTWP-1-x") == 1
    wt = tmp_path / "srv" / "work" / "verify"
    g.detached(mirror, wt, "origin/TESTWP-1-x")
    conflicts = g.merge_base_in(wt, "develop", ("bot", "bot@example.test"), commit=False)
    assert conflicts == (["a.txt"] if conflicting else [])
    assert g.behind(wt, "develop") == 1


class FakeGit:
    """Verify-side Git: the branch is behind until GitHub updates it."""

    def __init__(self, behind_before=1, conflicts=None, appears_after=1):
        self.behind_count = behind_before
        self.conflicts = conflicts or []
        self.appears_after = appears_after
        self.fetches = 0
        self.detached_refs = []
        self.updated = False

    def __call__(self, *_a, **_k):
        return self

    def mirror(self, repo, token, _transport=""):
        self.fetches += 1
        return "mirror"

    def remote_branch_exists(self, *_a):
        return True

    def detached(self, _mirror, _path, ref):
        self.detached_refs.append(ref)
        return "new-head" if self.updated else "old-head"

    def behind(self, path, base, ref="HEAD"):
        if path == "mirror" or self.updated:
            return 0 if self.fetches > self.appears_after else self.behind_count
        return self.behind_count

    def merge_base_in(self, _path, base, _identity, commit=False):
        return [] if self.updated else list(self.conflicts)

    def abort_merge(self, _path):
        pass

    def scrub(self, _path):
        pass


def project():
    return {"key": "JOOWP", "repo": "jooservices/wordpress-core", "base": "main", "checks": ["pytest"],
            "work_minutes": {"hard": 60}, "merge": {"method": "squash", "auto": True, "require_human": False}}


def pr_info():
    return {"number": 4, "branch": "JOOWP-4-work", "url": "https://github.com/jooservices/wordpress-core/pull/4",
            "head_sha": "old-head"}


def test_clean_behind_pull_request_is_updated_by_ai_flow_and_verified_on_the_new_head(monkeypatch, tmp_path):
    git = FakeGit(appears_after=2)
    monkeypatch.setattr(verify.time, "sleep", lambda _s: None)
    monkeypatch.setattr(verify, "Git", git)
    monkeypatch.setattr(verify, "get_adapter", lambda _p: _adapter("VERDICT: PASS\nCHECKS: pytest passed"))

    def update(_body):
        git.updated = True
        return {"ok": True, "message": "202"}

    seen_ci = []
    api = _API({"/v1/github/token": {"token": "t"}, "/v1/github/update-branch": update,
                "/v1/github/checks": lambda body: {"checks": []},
                "/v1/github/merge": {"merged": True, "sha": "b" * 40}})
    original = api.call

    def call(method, path, body=None, params=None):
        if path == "/v1/github/checks":
            seen_ci.append(params["sha"])
        return original(method, path, body, params)

    api.call = call
    result = verify.run_verify(api, _claim(project()), pr_info(), str(tmp_path), "https://control", 3600, [])
    assert result["outcome"] == "merged"
    assert [p for _, p, _, _ in api.calls].count("/v1/github/update-branch") == 1
    assert result["details"]["auto_updated"] is True
    assert git.detached_refs[-1] == "origin/JOOWP-4-work" and seen_ci == ["new-head"]
    assert not any(p == "/v1/jira/JOOWP-4/fields" and (b or {}).get("add_labels") for _, p, b, _ in api.calls)


def test_real_conflict_goes_back_to_a_developer(monkeypatch, tmp_path):
    git = FakeGit(conflicts=["a.txt"])
    monkeypatch.setattr(verify, "Git", git)
    api = _API({"/v1/github/token": {"token": "t"}, "/v1/github/update-branch": {"ok": False, "message": "422"}})
    result = verify.run_verify(api, _claim(project()), pr_info(), str(tmp_path), "https://control", 3600, [])
    assert result["outcome"] == "rework" and result["summary"].startswith("conflicts with main")
    assert result["details"]["reason"] == "conflict"
    assert not any(p == "/v1/github/update-branch" for _, p, _, _ in api.calls)  # a conflict is not auto-updated


def test_refused_update_without_conflict_still_verifies_the_merged_tree(monkeypatch, tmp_path):
    git = FakeGit()
    monkeypatch.setattr(verify, "Git", git)
    monkeypatch.setattr(verify, "get_adapter", lambda _p: _adapter("VERDICT: PASS\nCHECKS: ok"))
    api = _API({"/v1/github/token": {"token": "t"}, "/v1/github/update-branch": {"ok": False, "message": "403"},
                "/v1/github/checks": {"checks": []}, "/v1/github/merge": {"merged": True, "sha": "c" * 40}})
    result = verify.run_verify(api, _claim(project()), pr_info(), str(tmp_path), "https://control", 3600, [])
    assert result["outcome"] == "merged" and result["details"]["auto_updated"] is False


def test_github_update_endpoint_updates_a_verify_branch(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-20")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "claude-01"}, headers=cc).json()
    pr = client.post("/v1/github/pr", json={"run_id": r["run_id"], "fence": r["fence"], "repo": "test-aiflow-wp",
                                            "head": "TESTWP-20-x", "base": "develop", "title": "TESTWP-20: x"}, headers=cc).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "pr_url": pr["url"],
                                                        "branch": "TESTWP-20-x", "summary": "built"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-20'")
    v = client.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "codex-01", "step": "verify"},
                    headers=n1).json()
    # the pull request is behind: the first merge call updates the branch instead of merging
    ctx.github.pr_data = {1: {"number": 1, "base": {"ref": "develop"}, "head": {"ref": "TESTWP-20-x", "sha": "s1"}}}
    ctx.github.compare_results[("test-aiflow-wp", "develop", "TESTWP-20-x")] = {"behind_by": 2}
    first = client.post("/v1/github/merge", json={"run_id": v["run_id"], "fence": v["fence"], "number": 1}, headers=n1).json()
    assert first["updated"] and not first["merged"] and ctx.github.branch_updates == [("test-aiflow-wp", 1)]
    ctx.github.compare_results[("test-aiflow-wp", "develop", "TESTWP-20-x")] = {"behind_by": 0}
    second = client.post("/v1/github/merge", json={"run_id": v["run_id"], "fence": v["fence"], "number": 1}, headers=n1).json()
    assert second["merged"]


# --- AC3: one file per topic -----------------------------------------------------------------------------------------

def test_topics_are_one_line_files_and_the_index_is_generated():
    files = sorted((ROOT / "deploy/v4/topics").glob("*.md"))
    assert files and all(len(f.read_text().strip().splitlines()) == 1 for f in files)
    index = topics.index()
    assert "(../../docs/v4/merge-conflicts.md)" in index
    assert "(../../docs/v4/shared-accounts.md)" in index  # older README lines are part of the index
    for line in topics.topic_lines():
        link = line.split("](", 1)[1].split(")", 1)[0]
        assert (ROOT / "deploy/v4/topics" / link).resolve().exists()


def test_two_tickets_adding_topics_do_not_conflict(tmp_path):
    repo = tmp_path / "r"
    (repo / "deploy/v4/topics").mkdir(parents=True)
    sh(repo, "init", "-q", "-b", "develop")
    (repo / "deploy/v4/topics/base.md").write_text("- [Base](../../../docs/v4/base.md): base\n")
    sh(repo, "add", "-A"); sh(repo, "commit", "-q", "-m", "init")
    for name in ("one", "two"):
        sh(repo, "checkout", "-q", "-b", name, "develop")
        (repo / f"deploy/v4/topics/{name}.md").write_text(f"- [{name}](../../../docs/v4/{name}.md): {name}\n")
        sh(repo, "add", "-A"); sh(repo, "commit", "-q", "-m", name)
    sh(repo, "checkout", "-q", "one")
    sh(repo, "merge", "-q", "--no-edit", "two")  # raises when the merge conflicts
    assert len(topics.topic_lines(repo / "deploy/v4/topics")) == 3


def test_a_topic_file_with_two_lines_is_refused(tmp_path):
    (tmp_path / "x.md").write_text("- [A](a.md): a\n- [B](b.md): b\n")
    with pytest.raises(ValueError):
        topics.topic_lines(tmp_path)


# --- AC4: merge queue -------------------------------------------------------------------------------------------------

def test_queue_gives_the_turn_in_order_and_hands_over_after_a_merge(db):
    assert merge_queue.turn(db, "o/r", 1, "TESTWP-1")["turn"]
    second = merge_queue.turn(db, "o/r", 2, "TESTWP-2")
    assert not second["turn"] and second["position"] == 2 and second["ahead"] == ["TESTWP-1"]
    assert merge_queue.turn(db, "o/other", 9, "TESTWP-9")["turn"]  # queues are per repository
    merge_queue.finish(db, "o/r", 1, merged=True)
    assert merge_queue.turn(db, "o/r", 2, "TESTWP-2")["turn"]
    assert [e["number"] for e in merge_queue.snapshot(db, "o/r")] == [2]


def test_a_silent_or_finished_run_cannot_block_the_queue(db):
    merge_queue.turn(db, "o/r", 1, "TESTWP-1")
    db.run("UPDATE merge_queue SET touched_at = now() - interval '30 minutes' WHERE number=1")
    assert merge_queue.turn(db, "o/r", 2, "TESTWP-2")["turn"]
    rid = str(uuid.uuid4())
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status)"
           " VALUES (%s,'TESTWP-3','TESTWP','verify','codex-01','codex','cc',1,'finished')", (rid,))
    merge_queue.turn(db, "o/r", 3, "TESTWP-3", rid)  # 2 is first; 3 joins behind it, but its own run has ended
    assert merge_queue.turn(db, "o/r", 4, "TESTWP-4")["ahead"] == ["TESTWP-2"]


class QueueGitHub:
    def __init__(self, behind=0, update_ok=True, failure="conflict"):
        self.behind, self.update_ok, self.updates, self.failure = behind, update_ok, 0, failure

    def pr(self, repo, number):
        return {"base": {"ref": "develop"}, "head": {"ref": f"b{number}", "sha": f"sha{number}-{self.updates}"}}

    def compare_branches(self, repo, base, head):
        return {"behind_by": self.behind}

    def update_branch(self, repo, number):
        self.updates += 1
        if self.update_ok:
            self.behind = 0
        if self.update_ok:
            return {"ok": True, "conflict": False, "status": 202, "message": "202"}
        if self.failure == "conflict":
            return {"ok": False, "conflict": True, "status": 422, "message": "422: merge conflict"}
        return {"ok": False, "conflict": False, "status": 403, "message": "403: Resource not accessible"}


def test_merge_next_serializes_and_rebases_on_the_previous_merge(db):
    gh = QueueGitHub(behind=1)
    merged = []
    merge = lambda n: (lambda: merged.append(n) or {"merged": True, "sha": f"m{n}"})
    # PR 2 is ready second: it waits for PR 1 and is not sent back
    first = merge_queue.merge_next(db, gh, "o/r", 1, "TESTWP-1", "", merge(1))
    assert first["updated"] and not merged  # brought up to date on its turn, CI runs on the new head
    waiting = merge_queue.merge_next(db, gh, "o/r", 2, "TESTWP-2", "", merge(2))
    assert waiting["queued"] and waiting["position"] == 2 and "TESTWP-1" in waiting["message"]
    assert merge_queue.merge_next(db, gh, "o/r", 1, "TESTWP-1", "", merge(1))["merged"]
    gh.behind = 1  # PR 1 moved the base: PR 2 is behind now and is updated before it merges
    second = merge_queue.merge_next(db, gh, "o/r", 2, "TESTWP-2", "", merge(2))
    assert second["updated"]
    assert merge_queue.merge_next(db, gh, "o/r", 2, "TESTWP-2", "", merge(2))["merged"]
    assert merged == [1, 2]


def test_merge_next_reports_a_real_conflict_and_frees_the_queue(db):
    gh = QueueGitHub(behind=3, update_ok=False)
    res = merge_queue.merge_next(db, gh, "o/r", 1, "TESTWP-1", "", lambda: {"merged": True})
    assert res["conflict"] and not res["merged"]
    assert merge_queue.turn(db, "o/r", 2, "TESTWP-2")["turn"]


def test_a_failed_update_that_is_not_a_conflict_does_not_go_back_to_a_developer(db):
    gh = QueueGitHub(behind=3, update_ok=False, failure="permission")
    res = merge_queue.merge_next(db, gh, "o/r", 1, "TESTWP-1", "", lambda: {"merged": True})
    assert not res["merged"] and not res.get("conflict") and res["update_failed"]
    assert "403" in res["message"]
    assert merge_queue.turn(db, "o/r", 2, "TESTWP-2")["turn"]  # the queue is free again


def test_verify_does_not_turn_an_update_failure_into_a_conflict(monkeypatch):
    monkeypatch.setattr(verify.time, "sleep", lambda _s: None)
    api = _API({"/v1/github/merge": {"merged": False, "update_failed": True, "message": "could not update"}})
    hb = SimpleNamespace(lost=SimpleNamespace(is_set=lambda: False), progress={})
    out = verify.merge_with_retries(api, _claim(project()), pr_info(), "t", "squash", hb)
    assert not out["merged"] and not out.get("conflict") and out["update_failed"]


def test_concurrent_merge_requests_merge_one_at_a_time(db):
    """Both calls find themselves first in the queue; the repository lock lets only one merge at a time."""
    entered, release = threading.Event(), threading.Event()
    active, peak, merged = [], [], []

    def slow_merge(n):
        def merge():
            active.append(n)
            peak.append(len(active))
            entered.set()
            release.wait(5)
            active.remove(n)
            merged.append(n)
            return {"merged": True, "sha": f"m{n}"}
        return merge

    gh = QueueGitHub()
    results = {}
    first = threading.Thread(target=lambda: results.update(
        a=merge_queue.merge_next(db, gh, "o/r", 1, "TESTWP-1", "", slow_merge(1))))
    first.start()
    assert entered.wait(5)
    # a second request for the same repository arrives while the first merge is still running
    db.run("UPDATE merge_queue SET enqueued_at = now() + interval '1 second' WHERE number=1")  # it would see itself first
    second = merge_queue.merge_next(db, gh, "o/r", 2, "TESTWP-2", "", slow_merge(2))
    assert not second["merged"] and second["queued"] and not merged
    release.set()
    first.join(5)
    assert results["a"]["merged"] and max(peak) == 1
    # another repository is not held up by this lock
    with merge_queue.repo_lock(db, "o/other") as other:
        assert other
    with merge_queue.repo_lock(db, "o/r") as again:
        assert again  # released after the merge


def test_ready_to_merge_and_verify_merges_share_the_repository_queue(client, ctx):
    ctx.github.pr_data = {}

    def seed(key, status, number, sha):
        add_ticket(ctx.db, key, status=status)
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,details)"
                   " VALUES (%s,%s,'TESTWP','dev','claude-01','claude','node1',1,'finished','done',%s,%s)",
                   (str(uuid.uuid4()), key, f"https://github.com/jooservices/test-aiflow-wp/pull/{number}", J({})))
        ctx.jira.status_of[key] = status
        ctx.github.pr_data[number] = {"number": number, "state": "open", "merged": False,
                                      "mergeable_state": "clean", "head": {"sha": sha}}
        ctx.github.check_data = {**getattr(ctx.github, "check_data", {}), sha: [
            {"name": "Unit", "state": "success", "url": ""}]}

    seed("TESTWP-801", "Ready to merge", 801, "ready-head")
    seed("TESTWP-802", "In Verify", 802, "verify-head")
    node = join(client, "node1")
    verify_run = client.post("/v1/work/claim", json={"ticket": "TESTWP-802", "account": "codex-01", "step": "verify"},
                             headers=node).json()
    assert verify_run["claimed"], verify_run

    entered, release = threading.Event(), threading.Event()
    active, peak, merged = [], [], []
    real_merge = ctx.github.merge_pr
    state_lock = threading.Lock()

    def slow_merge(repo, number, title="", method="squash", sha=""):
        with state_lock:
            active.append(number)
            peak.append(len(active))
        if number == 801:
            entered.set()
            assert release.wait(5), "the test did not release the first merge"
        try:
            return real_merge(repo, number, title, method, sha)
        finally:
            with state_lock:
                active.remove(number)
                merged.append(number)

    ctx.github.merge_pr = slow_merge
    ready_result = {}
    ready_thread = threading.Thread(target=lambda: ready_result.setdefault("keys", merge_flow.merge_ready(ctx)))
    ready_thread.start()
    try:
        assert entered.wait(5), "Ready to merge did not enter its queued merge"
        queued = client.post("/v1/github/merge", json={"run_id": verify_run["run_id"],
                                  "fence": verify_run["fence"], "number": 802,
                                  "title": "TESTWP-802: Summary", "method": "squash"}, headers=node)
        assert queued.status_code == 200 and queued.json()["queued"] is True
        assert merged == []
    finally:
        release.set()
        ready_thread.join(10)

    assert not ready_thread.is_alive()
    assert ready_result["keys"] == ["TESTWP-801"]
    verified = client.post("/v1/github/merge", json={"run_id": verify_run["run_id"],
                               "fence": verify_run["fence"], "number": 802,
                               "title": "TESTWP-802: Summary", "method": "squash"}, headers=node)
    assert verified.status_code == 200 and verified.json()["merged"] is True
    assert merged == [801, 802] and max(peak) == 1


def test_the_repository_lock_is_released_when_the_merge_raises(db):
    def boom():
        raise RuntimeError("github down")

    with pytest.raises(RuntimeError):
        merge_queue.merge_next(db, QueueGitHub(), "o/r", 1, "TESTWP-1", "", boom)
    with merge_queue.repo_lock(db, "o/r") as locked:
        assert locked


def test_verify_waits_in_the_queue_then_merges(monkeypatch):
    answers = iter([{"merged": False, "queued": True, "position": 2, "message": "behind X"},
                    {"merged": False, "queued": True, "position": 2, "message": "behind X"},
                    {"merged": False, "updated": True, "head_sha": "n1"},
                    {"merged": True, "sha": "s"}])
    sleeps = []
    monkeypatch.setattr(verify.time, "sleep", lambda s: sleeps.append(s))
    api = _API({"/v1/github/merge": lambda _b: next(answers), "/v1/github/checks": {"checks": []}})
    hb = SimpleNamespace(lost=SimpleNamespace(is_set=lambda: False), progress={})
    out = verify.merge_with_retries(api, _claim(project()), pr_info(), "t", "squash", hb)
    assert out["merged"] and sleeps.count(verify.QUEUE_POLL_S) == 2


def test_verify_turns_a_queue_conflict_into_rework(monkeypatch, tmp_path):
    git = FakeGit(behind_before=0)
    monkeypatch.setattr(verify, "Git", git)
    monkeypatch.setattr(verify, "get_adapter", lambda _p: _adapter("VERDICT: PASS\nCHECKS: ok"))
    api = _API({"/v1/github/token": {"token": "t"}, "/v1/github/checks": {"checks": []},
                "/v1/github/merge": {"merged": False, "conflict": True, "message": "422"}})
    result = verify.run_verify(api, _claim(project()), pr_info(), str(tmp_path), "https://control", 3600, [])
    assert result["outcome"] == "rework" and result["details"]["reason"] == "conflict"
    assert result["summary"].startswith("conflicts with main")


def test_verify_gives_up_after_the_queue_wait(monkeypatch):
    monkeypatch.setattr(verify.time, "sleep", lambda _s: None)
    api = _API({"/v1/github/merge": {"merged": False, "queued": True, "position": 3, "message": "behind X"}})
    hb = SimpleNamespace(lost=SimpleNamespace(is_set=lambda: False), progress={})
    out = verify.merge_with_retries(api, _claim(project()), pr_info(), "t", "squash", hb, queue_wait_s=-1)
    assert not out["merged"] and "merge queue" in out["message"]


# --- AC5: share and time lost to conflicts -----------------------------------------------------------------------------

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def mk(ticket, step, outcome, days_ago, minutes, summary="", details=None, hour=1):
    start = NOW - timedelta(days=days_ago, hours=hour)
    return {"id": str(uuid.uuid4()), "ticket_key": ticket, "step": step, "outcome": outcome, "summary": summary,
            "details": details or {}, "started_at": start, "ended_at": start + timedelta(minutes=minutes)}


def test_share_and_time_follow_the_definition():
    conflict = "conflicts with develop: a.txt"
    runs = [
        # TESTWP-1: conflict (10 min), rework dev (30), re-verification that merges (20) -> 60 min of repeated work
        mk("TESTWP-1", "verify", "rework", 3, 10, conflict, hour=5),
        mk("TESTWP-1", "dev", "done", 3, 30, hour=4),
        mk("TESTWP-1", "verify", "merged", 3, 20, hour=3),
        # TESTWP-2: failed review (not a conflict), TESTWP-3: merged after an automatic clean update
        mk("TESTWP-2", "verify", "rework", 2, 15, "verify failed: bug"),
        mk("TESTWP-3", "verify", "merged", 2, 12, details={"auto_updated": True}),
        # TESTWP-4: conflict again in the re-verification: the shared run is counted once
        mk("TESTWP-4", "verify", "rework", 1, 5, conflict, details={"reason": "conflict"}, hour=6),
        mk("TESTWP-4", "dev", "done", 1, 10, hour=5),
        mk("TESTWP-4", "verify", "rework", 1, 5, conflict, hour=4),
        mk("TESTWP-4", "dev", "done", 1, 10, hour=3),
        mk("TESTWP-4", "verify", "merged", 1, 5, hour=2),
        mk("TESTWP-5", "verify", "failed", 1, 3, "verify worker error"),
        # an ordinary dev run is not a verification
        mk("TESTWP-6", "dev", "done", 1, 40),
    ]
    out = conflict_loss.calculate(runs, NOW, days=7)
    cur = out["current"]
    # verifications: 3+... TESTWP-1:2, TESTWP-2:1, TESTWP-3:1, TESTWP-4:3, TESTWP-5:1 -> 8; conflicts: 1 + 2 = 3
    assert (cur["verifications"], cur["conflicts"]) == (8, 3) and cur["share_percent"] == 37.5
    # TESTWP-1: 10+30+20; TESTWP-4: (5+10+5) + (5+10+5) with the middle verify shared: 5+10+5+10+5 = 35
    assert cur["seconds"] == (60 + 35) * 60 and cur["average_seconds"] == round((95 * 60) / 3)
    assert cur["clean_updates"] == 1
    assert "automatically" in out["definition"] and out["bucket"] == "day"
    assert len(out["trend"]) == 7 and sum(t["conflicts"] for t in out["trend"]) == 3
    assert next(t for t in out["trend"] if t["period"] == "2026-10-17")["share_percent"] == 50.0


def test_previous_period_and_weekly_trend_are_compared():
    conflict = "conflicts with develop: a.txt"
    runs = [mk("TESTWP-1", "verify", "rework", 10, 10, conflict), mk("TESTWP-2", "verify", "merged", 9, 10),
            mk("TESTWP-3", "verify", "merged", 8, 10), mk("TESTWP-4", "verify", "merged", 7, 10),
            mk("TESTWP-5", "verify", "merged", 1, 10), mk("TESTWP-6", "verify", "merged", 2, 10)]
    out = conflict_loss.calculate(runs, NOW, days=7, bucket="week")
    assert out["previous"]["share_percent"] == 25.0 and out["current"]["share_percent"] == 0.0
    assert out["change"]["share_points"] == -25.0 and out["change"]["seconds"] == -600
    assert all(datetime.fromisoformat(t["period"]).weekday() == 0 for t in out["trend"])


def test_no_verifications_gives_zero_not_an_error():
    out = conflict_loss.calculate([], NOW)
    assert out["current"]["share_percent"] == 0.0 and out["current"]["average_seconds"] == 0


def test_conflicts_endpoint_reads_the_runs_and_the_console_shows_it(client, ctx):
    now = datetime.now(timezone.utc)
    for ticket, outcome, summary, minutes in (("TESTWP-1", "rework", "conflicts with develop: a", 10),
                                             ("TESTWP-2", "merged", "", 5)):
        started = now - timedelta(hours=2)
        ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, node, fence, status, outcome,"
                   " summary, started_at, ended_at) VALUES (%s,%s,'TESTWP','verify','codex-01','codex','cc',1,'finished',"
                   "%s,%s,%s,%s)", (str(uuid.uuid4()), ticket, outcome, summary, started, started + timedelta(minutes=minutes)))
    data = client.get("/v1/analytics/conflicts", params={"days": 7}).json()
    assert data["current"]["conflicts"] == 1 and data["current"]["verifications"] == 2
    assert data["current"]["share_percent"] == 50.0 and data["current"]["seconds"] == 600
    assert client.get("/v1/analytics/conflicts", params={"project": "NOPE"}).json()["current"]["verifications"] == 0
    page = (ROOT / "aiflow/v4/control/ui/console.html").read_text()
    assert "/v1/analytics/conflicts" in page and "Lost to merge conflicts" in page and "c.definition" in page


def test_a_ticket_waiting_for_rework_with_an_open_pull_request_holds_no_hot_file(db):
    add_ticket(db, "TESTWP-1", status="To Do")
    db.run("UPDATE tickets SET pr_state='open' WHERE key='TESTWP-1'")
    add_ticket(db, "TESTWP-2", status="In Verify")
    describe(db, "TESTWP-1", "Card", "Edit " + CONSOLE)
    describe(db, "TESTWP-2", "Card two", "Edit " + CONSOLE)

    flying = hotfiles.in_flight_hot_files(db, lambda ticket: hotfiles.predict(db, ticket, "")) 

    assert "TESTWP-1" not in flying and "TESTWP-2" in flying
    assert hotfiles.reserve(db, "TESTWP-1", "gone", [CONSOLE]) is None
    assert hotfiles.reserve(db, "TESTWP-3", "run-3", [CONSOLE]) == ("TESTWP-1", [CONSOLE])  # fresh claim still counts
    db.run("UPDATE hot_file_reservations SET reserved_at = now() - interval '1 hour'")
    assert hotfiles.reserve(db, "TESTWP-3", "run-3", [CONSOLE]) is None  # an open pull request alone does not keep it


def test_blocking_ticket_waits_until_every_slot_of_the_file_is_taken():
    footprint = {"hot": [CONSOLE]}
    in_flight = {"TESTWP-1": [CONSOLE], "TESTWP-2": [CONSOLE]}

    assert hotfiles.blocking_ticket(footprint, in_flight) == ("TESTWP-1", [CONSOLE])  # one slot: blocked
    assert hotfiles.blocking_ticket(footprint, in_flight, slots=3) is None  # two of three taken
    assert hotfiles.blocking_ticket(footprint, in_flight, slots=2) == ("TESTWP-1", [CONSOLE])  # both taken


def test_reservations_hold_as_many_tickets_as_there_are_slots(db):
    assert hotfiles.reserve(db, "TESTWP-1", "run-1", [CONSOLE], slots=2) is None
    assert hotfiles.reserve(db, "TESTWP-2", "run-2", [CONSOLE], slots=2) is None
    assert hotfiles.reserve(db, "TESTWP-3", "run-3", [CONSOLE], slots=2) == ("TESTWP-1", [CONSOLE])
    assert hotfiles.reserve(db, "TESTWP-3", "run-3", [CONSOLE], slots=3) is None  # a third slot: taken

    hotfiles.unreserve(db, "run-2")

    assert hotfiles.reserve(db, "TESTWP-4", "run-4", [CONSOLE], slots=3) is None
    assert db.one("SELECT count(*) AS n FROM hot_file_reservations WHERE path=%s", (CONSOLE,))["n"] == 3
