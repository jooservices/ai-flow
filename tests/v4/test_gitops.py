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


def test_verdict_and_findings_parsing():
    text = "looked at it\nCHECKS: phpunit ok\nVERDICT: FAIL\nFINDINGS:\n- core/a.php: missing escaping\n- tests: none for AC2\n"
    assert VERDICT.search(text).group(1) == "FAIL"
    assert findings_of(text) == "- core/a.php: missing escaping\n- tests: none for AC2"


def test_pr_title_is_a_conventional_commit_with_the_key():
    from aiflow.v4.worker.dev import pr_title
    assert pr_title({"key": "TESTWP-4", "summary": "monorepo scaffold.", "type": "Story"}) == "feat: Monorepo scaffold (TESTWP-4)"
    assert pr_title({"key": "TESTWP-9", "summary": "Broken link", "type": "Bug"}).startswith("fix: Broken link")


def test_conflict_markers_are_found_only_at_line_starts():
    from aiflow.v4.worker.dev import CONFLICT_MARKER
    assert CONFLICT_MARKER.search("a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> origin/develop\n")
    assert not CONFLICT_MARKER.search('text = "conflict markers (<<<<<<<, =======, >>>>>>>)"\n')
