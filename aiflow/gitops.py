"""Worktrees, push and merge for a ticket branch. Uses the Workspaces `scripts/git/worktree` helper."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError((result.stdout + result.stderr).strip()[:500])
    return result.stdout.strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "work"


class Repo:
    def __init__(self, root: Path, name: str, company: str = "JOOservices") -> None:
        self.root, self.name, self.company = root, name, company
        self.path = root / company / "projects" / name

    def exists(self) -> bool:
        return (self.path / ".git").exists()

    def has_remote(self) -> bool:
        return bool(git(self.path, "remote", check=False))

    def _helper(self, *args: str) -> dict:
        result = subprocess.run([str(self.root / "scripts" / "git" / "worktree"), *args], capture_output=True, text=True, cwd=self.root)
        if result.returncode != 0:
            raise GitError((result.stdout + result.stderr).strip()[:500])
        return json.loads(result.stdout)

    def find_worktree(self, key: str) -> tuple[Path, str] | None:
        out = git(self.path, "worktree", "list", "--porcelain")
        for block in out.split("\n\n"):
            fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            branch = fields.get("branch", "").removeprefix("refs/heads/")
            if branch.startswith(f"{key}-"):
                return Path(fields["worktree"]), branch
        return None

    def refresh_base(self, base: str) -> None:
        """Bring the local base branch up to date with origin (fast-forward only)."""
        if not self.has_remote():
            return
        git(self.path, "fetch", "origin", base)
        remote = f"origin/{base}"
        if git(self.path, "rev-parse", base) == git(self.path, "rev-parse", remote):
            return
        ancestor = subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", base, remote]).returncode == 0
        if not ancestor:
            return  # local base has commits origin lacks; the merge step pushes them
        current = git(self.path, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        if current == base and not git(self.path, "status", "--porcelain"):
            git(self.path, "merge", "--ff-only", remote)
        else:
            git(self.path, "update-ref", f"refs/heads/{base}", git(self.path, "rev-parse", remote))

    def remote_branch(self, key: str) -> str | None:
        """Name of the ticket branch already pushed to origin (work started on another machine)."""
        if not self.has_remote():
            return None
        out = git(self.path, "ls-remote", "--heads", "origin", f"{key}-*", check=False)
        names = [line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line]
        return names[0] if names else None

    def ensure_worktree(self, key: str, summary: str, base: str) -> tuple[Path, str]:
        found = self.find_worktree(key)
        if found:
            return found
        self.refresh_base(base)
        pushed = self.remote_branch(key)
        if pushed:
            git(self.path, "fetch", "origin", pushed)
            # The helper creates the branch from a local base (so its merged-check later uses `base`); the new worktree is
            # empty, so moving it onto the pushed commit loses nothing.
            info = self._helper("create", "--repo", f"{self.company}/projects/{self.name}", "--company", self.company,
                                "--ticket", key, "--description", pushed.removeprefix(f"{key}-"), "--base", base)
            workdir = Path(info["path"])
            git(workdir, "reset", "--hard", f"origin/{pushed}")
            git(workdir, "branch", "--set-upstream-to", f"origin/{pushed}", check=False)
            return Path(info["path"]), info["branch"]
        info = self._helper("create", "--repo", f"{self.company}/projects/{self.name}", "--company", self.company,
                            "--ticket", key, "--description", slug(summary), "--base", base)
        return Path(info["path"]), info["branch"]

    def ahead(self, workdir: Path, base: str) -> int:
        return int(git(workdir, "rev-list", "--count", f"{base}..HEAD"))

    def push(self, workdir: Path, branch: str) -> bool:
        if not self.has_remote():
            return False
        git(workdir, "push", "-u", "origin", branch)
        return True

    def merge(self, key: str, branch: str, base: str) -> str:
        """Merge `branch` into `base` (no-ff) in a throw-away worktree; never checks out in the main checkout."""
        self.refresh_base(base)
        tmp = self.root / ".tmp" / f"aiflow-merge-{key}"
        git(self.path, "worktree", "add", "--detach", str(tmp), base)
        try:
            try:
                git(tmp, "merge", "--no-ff", "-m", f"{key}: merge {branch}", branch)
            except GitError as error:
                git(tmp, "merge", "--abort", check=False)
                raise GitError(f"merge conflict or failure: {error}") from error
            merged = git(tmp, "rev-parse", "HEAD")
        finally:
            git(self.path, "worktree", "remove", "--force", str(tmp), check=False)
        current = git(self.path, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        clean = not git(self.path, "status", "--porcelain")
        if current == base and clean:
            git(self.path, "merge", "--ff-only", merged)
        else:
            git(self.path, "update-ref", f"refs/heads/{base}", merged)
        if self.has_remote():
            git(self.path, "push", "origin", base)
        return merged

    def cleanup(self, key: str) -> None:
        found = self.find_worktree(key)
        if found:
            self._helper("remove", "--path", str(found[0]), "--delete-branch")
        pushed = self.remote_branch(key)
        if pushed:
            git(self.path, "push", "origin", "--delete", pushed, check=False)

    def open_pr(self, key: str, branch: str, base: str, title: str, body: str) -> str:
        """Push the ticket branch and open a pull request with gh; returns the PR URL."""
        git(self.path, "push", "-u", "origin", branch)
        result = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", f"{key}: {title}", "--body", body],
                                capture_output=True, text=True, cwd=self.path)
        if result.returncode != 0:
            raise GitError((result.stdout + result.stderr).strip()[:400])
        return result.stdout.strip().splitlines()[-1]
