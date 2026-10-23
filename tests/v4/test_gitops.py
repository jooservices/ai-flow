import os
import subprocess
from pathlib import Path

from aiflow.v4.worker.gitops import Git
from aiflow.v4.worker.verify import VERDICT, findings_of

ID = ("bot", "bot@example.com")


def sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
                        "PATH": "/usr/bin:/bin", "HOME": str(cwd)})


def make_origin(tmp: Path) -> Path:
    o = tmp / "origin"
    o.mkdir()
    sh(o, "init", "-q", "-b", "develop")
    (o / "a.txt").write_text("one\n")
    sh(o, "add", "-A"); sh(o, "commit", "-q", "-m", "init")
    sh(o, "checkout", "-q", "-b", "TESTWP-1-x")
    (o / "a.txt").write_text("branch\n")
    sh(o, "commit", "-q", "-am", "branch change")
    sh(o, "checkout", "-q", "develop")
    (o / "a.txt").write_text("develop\n")
    sh(o, "commit", "-q", "-am", "develop change")
    return o


def test_merge_base_reports_conflicts_and_detached_verify(tmp_path):
    o = make_origin(tmp_path)
    g = Git(str(tmp_path / "srv"))
    mirror = tmp_path / "srv" / "mirrors" / "m.git"
    mirror.parent.mkdir(parents=True)
    g.run("clone", "--bare", "-q", str(o), str(mirror))
    g.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    g.run("fetch", "-q", "origin", cwd=mirror)
    wt = tmp_path / "srv" / "work" / "dev"
    start = g.worktree(mirror, wt, "TESTWP-1-x", "develop")
    assert start == "origin/TESTWP-1-x"
    conflicts = g.merge_base_in(wt, "develop", ID)
    assert conflicts == ["a.txt"] and "<<<<<<<" in (wt / "a.txt").read_text()
    g.abort_merge(wt)
    vt = tmp_path / "srv" / "work" / "verify"
    head = g.detached(mirror, vt, "origin/TESTWP-1-x")
    assert len(head) == 40
    assert g.merge_base_in(vt, "develop", ID, commit=False) == ["a.txt"]


def test_mirror_uses_command_center_transport_url_without_putting_token_in_remote(tmp_path):
    g = Git(str(tmp_path / "srv"))
    mirror = tmp_path / "srv" / "mirrors" / "person__tool.git"
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/develop\n")
    calls = []
    g.run = lambda *args, **kwargs: calls.append((args, kwargs)) or ""
    transport = "http://control.example.test/v1/github/git/person/tool.git"
    g.mirror("person/tool", "opaque-capability", transport)
    assert calls[0][0] == ("remote", "set-url", "origin", transport)
    assert calls[0][1]["cwd"] == mirror
    assert calls[1][0] == ("fetch", "--prune", "--quiet", "origin")
    assert calls[1][1]["token"] == "opaque-capability"


def test_triage_checkout_is_detached_from_base_and_read_only(tmp_path):
    o = make_origin(tmp_path)
    g = Git(str(tmp_path / "srv"))
    mirror = tmp_path / "srv" / "mirrors" / "m.git"
    mirror.parent.mkdir(parents=True)
    g.run("clone", "--bare", "-q", str(o), str(mirror))
    g.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    g.run("fetch", "-q", "origin", cwd=mirror)
    wt = tmp_path / "srv" / "triage" / "TESTWP-1"
    calls = []
    run = g.run
    g.run = lambda *a, **k: calls.append(a) or run(*a, **k)
    head = g.readonly_worktree(mirror, wt, "origin/develop")
    g.run = run
    # root owns the checkout, git runs as the AI user: rev-parse must trust the path
    assert ("-c", f"safe.directory={wt}", "rev-parse", "HEAD") in calls
    # the Codex sandbox would otherwise try to create these in the read-only checkout
    assert (wt / ".codex").is_dir() and (wt / ".agents").is_dir()

    assert len(head) == 40 and (wt / "a.txt").read_text() == "develop\n"
    assert (wt / "a.txt").stat().st_uid == (0 if os.geteuid() == 0 else os.geteuid())
    assert (wt / "a.txt").stat().st_mode & 0o222 == 0
    assert wt.stat().st_mode & 0o222 == 0
    g.remove_worktree(mirror, wt)
    assert not wt.exists()


def test_verdict_and_findings_parsing():
    text = "looked at it\nCHECKS: phpunit ok\nVERDICT: FAIL\nFINDINGS:\n- core/a.php: missing escaping\n- tests: none for AC2\n"
    assert VERDICT.search(text).group(1) == "FAIL"
    assert findings_of(text) == "- core/a.php: missing escaping\n- tests: none for AC2"
    blocked = "CHECKS: none\nVERDICT: BLOCKED\nFINDINGS:\n- environment: bwrap: Can't mkdir\n"
    assert VERDICT.search(blocked).group(1) == "BLOCKED"


def test_pr_title_is_a_conventional_commit_with_the_key():
    from aiflow.v4.worker.dev import pr_title
    assert pr_title({"key": "TESTWP-4", "summary": "monorepo scaffold.", "type": "Story"}) == "feat: Monorepo scaffold (TESTWP-4)"
    assert pr_title({"key": "TESTWP-9", "summary": "Broken link", "type": "Bug"}).startswith("fix: Broken link")
    assert pr_title({"key": "TESTWP-87", "summary": "Điều kiện sẵn sàng release", "type": "Story"}) == (
        "feat: TESTWP-87: Điều kiện sẵn sàng release"
    )


def test_conflict_markers_are_found_only_at_line_starts():
    from aiflow.v4.worker.dev import CONFLICT_MARKER
    assert CONFLICT_MARKER.search("a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> origin/develop\n")
    assert not CONFLICT_MARKER.search('text = "conflict markers (<<<<<<<, =======, >>>>>>>)"\n')


def test_repository_change_between_runs_replaces_the_old_worktree(tmp_path):
    """JAIF-250: a worktree made for the old repository is never reused after the ticket's repository changes."""
    old_origin = make_origin(tmp_path)
    new_origin = tmp_path / "new_origin"
    new_origin.mkdir()
    sh(new_origin, "init", "-q", "-b", "develop")
    (new_origin / "app.txt").write_text("new repo\n")
    sh(new_origin, "add", "-A"); sh(new_origin, "commit", "-q", "-m", "init")
    g = Git(str(tmp_path / "srv"))
    mirrors = {}
    for name, origin in (("old__repo", old_origin), ("new__repo", new_origin)):
        m = tmp_path / "srv" / "mirrors" / f"{name}.git"
        m.parent.mkdir(parents=True, exist_ok=True)
        g.run("clone", "--bare", "-q", str(origin), str(m))
        g.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=m)
        g.run("fetch", "-q", "origin", cwd=m)
        mirrors[name] = m
    wt = tmp_path / "srv" / "work" / "TESTWP" / "TESTWP-2-y"
    # run 1: old repository, the AI commits work that is not pushed
    assert g.foreign_worktree(mirrors["old__repo"], wt, "TESTWP-2-y") is None
    g.worktree(mirrors["old__repo"], wt, "TESTWP-2-y", "develop")
    (wt / "work.txt").write_text("unpushed\n")
    g.commit_all(wt, "old repo work", ID)
    assert g.foreign_worktree(mirrors["old__repo"], wt, "TESTWP-2-y") is None  # same repository: reused
    # run 2: the repository changed
    moved = g.foreign_worktree(mirrors["new__repo"], wt, "TESTWP-2-y")
    assert moved and moved["old_repo"] == "old/repo" and not moved["pushed"]
    assert [x.split(" ", 1)[1] for x in moved["commits"]] == ["old repo work"]
    g.drop_foreign_worktree(wt, "TESTWP-2-y", Path(moved["old_mirror"]))
    assert not wt.exists()
    assert g.run("branch", "--list", "TESTWP-2-y", cwd=mirrors["old__repo"]).strip() == ""
    start = g.worktree(mirrors["new__repo"], wt, "TESTWP-2-y", "develop")
    assert start == "origin/develop"
    assert (wt / "app.txt").read_text() == "new repo\n" and not (wt / "work.txt").exists()
    assert g.worktree_mirror(wt) == mirrors["new__repo"]
