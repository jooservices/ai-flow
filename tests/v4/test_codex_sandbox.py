"""JAIF-18: Codex edits stay inside workspace-write; the worker commits linked worktrees afterward."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from aiflow.v4.adapters.base import RunSpec, scratch_root
from aiflow.v4.adapters.codex import CodexAdapter
from aiflow.v4.worker.gitops import Git


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(cwd),
                        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.test",
                        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.test"})


def test_codex_downgrades_danger_full_access_and_only_adds_scratch_root(tmp_path):
    home = tmp_path / "srv" / "accounts" / "codex-01"
    home.mkdir(parents=True)
    worktree = tmp_path / "srv" / "work" / "JAIF-18"
    worktree.mkdir(parents=True)
    spec = RunSpec(prompt="edit the ticket", cwd=str(worktree), account_home=str(home),
                   sandbox="danger-full-access")

    argv = CodexAdapter().argv(spec)

    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "danger-full-access" not in argv
    assert argv[argv.index("--cd") + 1] == str(worktree)
    roots_arg = next(arg for arg in argv if arg.startswith("sandbox_workspace_write.writable_roots="))
    assert json.loads(roots_arg.partition("=")[2]) == [str(scratch_root(spec))]


def test_worker_commits_codex_edits_in_a_linked_worktree_after_the_ai_exits(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "develop")
    (origin / "README.md").write_text("base\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "initial")

    srv = tmp_path / "srv"
    mirror = srv / "mirrors" / "example__repo.git"
    mirror.parent.mkdir(parents=True)
    git = Git(str(srv))
    git.run("clone", "--bare", "-q", str(origin), str(mirror))
    git.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=mirror)
    git.run("fetch", "-q", "origin", cwd=mirror)

    worktree = srv / "work" / "JAIF-18"
    git.worktree(mirror, worktree, "JAIF-18-sandbox", "develop")
    # This stands for edits made by Codex in cwd; the worker's Git operation happens after Codex exits.
    (worktree / "README.md").write_text("implemented\n")
    assert git.changed(worktree) == ["README.md"]

    commit = git.commit_all(worktree, "feat: contain Codex", ("AI Flow", "ai-flow@example.test"))

    assert len(commit) == 40
    assert git.run("show", "--format=%s", "--no-patch", cwd=worktree).strip() == "feat: contain Codex"
    assert git.changed(worktree) == []
