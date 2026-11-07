"""JAIF-336: mechanical returns (stale branch, docs/fast checks) caught before hand-over; Dashboard yield."""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import analytics, login
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import build_console_page
from aiflow.v4.worker import mechanical, verify
from aiflow.v4.worker.dev import CONFLICT_MARKER
from aiflow.v4.worker.gitops import Git

UTC = timezone.utc
ID = ("bot", "bot@example.test")


def sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


def day(back: int) -> datetime:
    today = datetime.now(UTC).date()
    return datetime.combine(today - timedelta(days=back), time(1, 0), tzinfo=UTC)


def add_run(db, *, back: int = 0, step: str = "dev", outcome: str = "done",
            tokens_in: int = 100, tokens_out: int = 50, project: str = "TESTWP",
            status: str = "finished", seconds: int | None = 600) -> None:
    started = day(back)
    ended = None if seconds is None else started + timedelta(seconds=seconds)
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
        "started_at,ended_at,tokens_in,tokens_out) VALUES (%s,%s,%s,%s,'claude-01','claude','cc',1,%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), f"{project}-1", project, step, status, outcome, started, ended,
         tokens_in, tokens_out),
    )


def mirror_of(origin: Path, srv: Path) -> tuple[Git, Path]:
    g = Git(str(srv))
    mirror = srv / "mirrors" / "m.git"
    mirror.parent.mkdir(parents=True)
    g.run("clone", "--bare", "-q", str(origin), str(mirror))
    g.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    g.run("fetch", "-q", "origin", cwd=mirror)
    return g, mirror


def test_fast_plan_excludes_service_backed_steps():
    names = [step.name for step in mechanical.fast_plan("develop")]
    assert "docs-check" in names and "quality" in names
    assert "v4" not in names and "scale-proof" not in names
    assert all(not step.needs_env for step in mechanical.fast_plan("develop"))


def test_identical_both_sides_conflict_is_resolved_automatically(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "develop")
    (repo / "a.txt").write_text("same-both\n")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", "init")
    g = Git(str(tmp_path / "srv"))
    sha = g.run("hash-object", "-w", str(repo / "a.txt"), cwd=repo).strip()
    payload = f"100644 {sha} 1\ta.txt\n100644 {sha} 2\ta.txt\n100644 {sha} 3\ta.txt\n"
    proc = subprocess.run(["git", "update-index", "--index-info"], cwd=str(repo), input=payload, text=True,
                          capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(repo)})
    assert proc.returncode == 0, proc.stderr
    assert g.unmerged_stages(repo, "a.txt") == {1: sha, 2: sha, 3: sha}
    assert g.resolve_trivial_conflicts(repo) == []
    assert not g.run("diff", "--name-only", "--diff-filter=U", cwd=repo, check=False).strip()
    assert (repo / "a.txt").read_text() == "same-both\n"


def test_real_conflict_remains_named(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    sh(origin, "init", "-q", "-b", "develop")
    (origin / "a.txt").write_text("base\n")
    sh(origin, "add", "-A")
    sh(origin, "commit", "-q", "-m", "init")
    sh(origin, "checkout", "-q", "-b", "TICKET-2-x")
    (origin / "a.txt").write_text("branch\n")
    sh(origin, "commit", "-q", "-am", "branch")
    sh(origin, "checkout", "-q", "develop")
    (origin / "a.txt").write_text("develop\n")
    sh(origin, "commit", "-q", "-am", "develop")
    g, mirror = mirror_of(origin, tmp_path / "srv")
    wt = tmp_path / "srv" / "work" / "dev"
    g.worktree(mirror, wt, "TICKET-2-x", "develop")
    assert g.merge_base_resolving(wt, "develop", ID) == ["a.txt"]
    assert CONFLICT_MARKER.search((wt / "a.txt").read_text())


def test_refresh_base_refuses_real_conflicts(monkeypatch, tmp_path):
    comments = []

    class FakeGit:
        def mirror(self, *_a, **_k):
            return "mirror"

        def merge_base_resolving(self, *_a, **_k):
            return ["docs/v4/topic.md", "aiflow/v4/control/api.py"]

        def abort_merge(self, _path):
            pass

        def behind(self, *_a, **_k):
            return 0

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="r1", fence=1, ticket={"key": "JAIF-336"}, account="a",
                              provider="claude", model="", identity=ID, step="dev"),
        project={"base": "develop", "key": "JAIF"}, repo="org/repo", branch="JAIF-336-x",
        git=FakeGit(), worktree=tmp_path, api=SimpleNamespace(call=lambda *a, **k: {"token": "t"}),
        heartbeat=SimpleNamespace(), started_at=0.0, result={}, key="JAIF-336",
        reporter=SimpleNamespace(comment=lambda **k: comments.append(k), transition=lambda **k: None),
        srv_root=str(tmp_path), run_as=[], env={},
    )
    monkeypatch.setattr(mechanical, "set_run_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(mechanical, "api_node", lambda _api: "node")
    out = mechanical.refresh_base(run)
    assert out["outcome"] == "failed" and out["error_kind"] == "conflict"
    assert out["details"]["conflict_files"][0] == "docs/v4/topic.md"
    assert "docs/v4/topic.md" in comments[0]["body"]


def test_checks_after_refresh_use_fast_plan_for_ai_flow(monkeypatch):
    seen = {}

    def fake_fast(run):
        seen["fast"] = True
        return ""

    def local_gate_runner(_run):
        seen["full"] = True
        return "full"

    run = SimpleNamespace(project={"local_gate": "ai-flow"})
    monkeypatch.setattr(mechanical, "run_fast_checks", fake_fast)
    assert mechanical.checks_after_refresh(run, local_gate_runner) == ""
    assert seen == {"fast": True}


def test_hand_over_refused_when_fast_checks_fail(monkeypatch, tmp_path):
    comments = []

    class FakeGit:
        def run(self, *args, **_k):
            return ""

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="r1", fence=1, ticket={"key": "JAIF-336", "summary": "x", "type": "Story"},
                              account="a", provider="claude", model="", identity=ID, step="dev"),
        project={"base": "develop", "key": "JAIF", "local_gate": "ai-flow"}, repo="org/repo",
        branch="JAIF-336-x", git=FakeGit(), worktree=tmp_path, key="JAIF-336",
        api=SimpleNamespace(call=lambda *a, **k: {"token": "t"}), heartbeat=SimpleNamespace(),
        started_at=0.0, result={}, srv_root=str(tmp_path), run_as=[], env={},
        reporter=SimpleNamespace(comment=lambda **k: comments.append(k), transition=lambda **k: None),
    )
    monkeypatch.setattr(mechanical, "set_run_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(mechanical, "api_node", lambda _api: "node")
    monkeypatch.setattr(mechanical.local_gate, "run", lambda *_a, **_k: SimpleNamespace(
        failed=SimpleNamespace(name="docs-check", tail="missing topic link"), incomplete=[]))
    monkeypatch.setattr(mechanical.gate_step, "failure_text", lambda _g: "step docs-check failed:\nmissing topic link")
    monkeypatch.setattr(mechanical.gate_step, "keep_failed_work", lambda *_a, **_k: "(not saved)")
    out = mechanical.run_fast_checks(run)
    assert out["outcome"] == "failed" and out["error_kind"] == "gate_failed"
    assert "docs-check" in out["summary"]
    assert "fast checks failed" in comments[0]["body"]


def test_hand_over_refused_when_branch_stays_behind(monkeypatch, tmp_path):
    class FakeGit:
        def mirror(self, *_a, **_k):
            return "mirror"

        def merge_base_resolving(self, *_a, **_k):
            return []

        def behind(self, *_a, **_k):
            return 2

    run = SimpleNamespace(
        claim=SimpleNamespace(run_id="r1", fence=1, ticket={"key": "JAIF-336"}, account="a",
                              provider="claude", model="", identity=ID, step="dev"),
        project={"base": "develop", "key": "JAIF"}, repo="org/repo", branch="JAIF-336-x",
        git=FakeGit(), worktree=tmp_path, api=SimpleNamespace(call=lambda *a, **k: {"token": "t"}),
        heartbeat=SimpleNamespace(), started_at=0.0, result={}, key="JAIF-336",
        reporter=SimpleNamespace(comment=lambda **k: None, transition=lambda **k: None),
        srv_root=str(tmp_path), run_as=[], env={},
    )
    monkeypatch.setattr(mechanical, "set_run_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(mechanical, "api_node", lambda _api: "node")
    out = mechanical.refresh_base(run)
    assert out["outcome"] == "failed" and out["error_kind"] == "stale_branch"


def test_verify_prepare_uses_merge_base_resolving(monkeypatch, tmp_path):
    class Git:
        def __init__(self, *_a, **_k):
            self.resolved = False

        def mirror(self, *_a, **_k):
            return "mirror"

        def remote_branch_exists(self, *_a):
            return True

        def detached(self, *_a):
            return "abc123456789"

        def merge_base_resolving(self, *_a, **_k):
            self.resolved = True
            return []

        def behind(self, *_a, **_k):
            return 0

        def scrub(self, *_a):
            pass

    git = Git()
    monkeypatch.setattr(verify, "Git", lambda *_a, **_k: git)
    state = SimpleNamespace(
        claim=SimpleNamespace(run_id="r", fence=1, identity=ID,
                              project={"key": "JAIF", "repo": "o/r", "base": "develop"},
                              ticket={"key": "JAIF-336"}),
        pr={"number": 1, "branch": "JAIF-336-x", "head_sha": "abc123456789"},
        api=SimpleNamespace(call=lambda *a, **k: {"token": "t"}),
        git=git, srv_root=str(tmp_path), heartbeat=SimpleNamespace(), result={},
        reporter=SimpleNamespace(comment=lambda **k: None), started_at=0.0,
    )
    monkeypatch.setattr(verify, "set_run_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(verify, "report_verify_started", lambda *_a, **_k: None)
    monkeypatch.setattr(verify, "worktree_path", lambda *_a, **_k: tmp_path / "wt")
    out = verify.prepare_verify(state)
    assert git.resolved and not isinstance(out, dict)


def test_yield_metrics_reject_invalid_days(db):
    with pytest.raises(ValueError):
        analytics.yield_metrics(db, 0)
    with pytest.raises(ValueError):
        analytics.yield_metrics(db, 91)


def test_yield_metrics_are_zero_when_no_runs_exist(db):
    data = analytics.yield_metrics(db, 14)
    assert data["days"] == 14
    assert (data["developer_runs"], data["merged"], data["merged_per_100_runs"],
            data["target_merged_per_100"]) == (0, 0, 0.0, 40.0)
    assert data["runs_per_merged"] is None and data["tokens_per_merged"] is None
    assert data["daily"] == []


def test_dashboard_yield_includes_per_day_ratios(client, ctx):
    # 5 completed developer runs, 2 merges → 40 merged per 100 runs (on target)
    for _ in range(5):
        add_run(ctx.db, outcome="done", tokens_in=100, tokens_out=50)
    add_run(ctx.db, step="verify", outcome="merged", tokens_in=0, tokens_out=0)
    add_run(ctx.db, step="verify", outcome="merged", tokens_in=0, tokens_out=0)
    add_run(ctx.db, outcome="", status="running", seconds=None)  # ignored
    add_run(ctx.db, project="OTHER", outcome="done")

    response = client.get("/v1/dashboard/yield", params={"days": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["developer_runs"] == 6  # includes OTHER
    assert data["merged"] == 2
    assert data["merged_per_100_runs"] == round(100 * 2 / 6, 2)
    assert data["runs_per_merged"] == round(6 / 2, 2)
    assert data["tokens_per_merged"] == round((5 * 150 + 150) / 2, 1)
    assert data["target_merged_per_100"] == 40.0
    assert data["daily"][0]["day"] == day(0).date().isoformat()
    assert client.get("/v1/dashboard/yield", params={"days": 0}).status_code == 422
    assert client.get("/v1/dashboard/yield", params={"days": 91}).status_code == 422


def test_dashboard_yield_respects_project_permissions(client, ctx):
    add_run(ctx.db, outcome="done")
    add_run(ctx.db, step="verify", outcome="merged")
    add_run(ctx.db, project="OTHER", outcome="done")
    add_run(ctx.db, project="OTHER", step="verify", outcome="merged")
    assert client.get("/v1/dashboard/yield").status_code == 200

    ctx.settings.console_login = True
    ctx.settings.console_session_secret = "test-dashboard-yield-secret-with-at-least-32-bytes!!"
    assert client.get("/v1/dashboard/yield").status_code == 401
    login.seed_user(ctx.db, "yield-limited@example.test", "business")
    ctx.db.run("UPDATE console_users SET project_keys=%s WHERE email=%s",
               (["TESTWP"], "yield-limited@example.test"))
    client.cookies.set(COOKIE_NAME, issue_session("yield-limited@example.test", "business", 2_000_000_000,
                                                  ctx.settings.console_session_secret))
    data = client.get("/v1/dashboard/yield").json()
    assert data["developer_runs"] == 1 and data["merged"] == 1
    assert data["merged_per_100_runs"] == 100.0


def test_dashboard_yield_panel_is_injected_and_fetches_the_yield_route():
    page = build_console_page()
    assert "function dashboardYieldPanel()" in page
    assert 'data-block="yield"' in page
    assert "Merged tickets per developer run" in page
    assert "Merged per 100 runs" in page and "Tokens per merged ticket" in page
    assert "fetch('/v1/dashboard/yield?days=14'" in page
    assert "loadDashboardYield(false)" in page
    html = page.split("function dashboardYieldPanel", 1)[0]
    assert 'data-block="yield"' not in html


def test_mechanical_returns_topic_is_linked():
    from aiflow.v4.core import topics
    assert any("mechanical-returns.md" in line for line in topics.topic_lines())
    assert (Path("docs/v4/mechanical-returns.md")).is_file()
