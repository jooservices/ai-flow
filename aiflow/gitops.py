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

    def github_slug(self) -> str:
        """`owner/name` of the origin repository on GitHub."""
        url = git(self.path, "remote", "get-url", "origin")
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if not match:
            raise GitError(f"origin of {self.name} is not a GitHub repository: {url}")
        return match.group(1)

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

    def push(self, workdir: Path, branch: str, lease: bool = False) -> bool:
        """Push the ticket branch. `lease` allows rewriting it (after a rebase) but only if nobody else moved it."""
        if not self.has_remote():
            return False
        git(workdir, "push", "-u", *(["--force-with-lease"] if lease else []), "origin", branch)
        return True

    def fetch(self, *refs: str) -> None:
        if self.has_remote():
            git(self.path, "fetch", "origin", *refs)

    def behind(self, base: str, branch: str) -> bool:
        """True when origin/<base> has commits that origin/<branch> lacks (the PR is not up to date)."""
        self.fetch(base, branch)
        return subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", f"origin/{base}", f"origin/{branch}"]).returncode != 0

    def remote_head(self, branch: str) -> str:
        return git(self.path, "rev-parse", f"origin/{branch}")

    def sync_worktree(self, workdir: Path, branch: str) -> None:
        """Make a clean worktree equal to origin/<branch> (the PR head may have moved: update-branch, rebase on another machine)."""
        self.fetch(branch)
        if git(workdir, "status", "--porcelain"):
            raise GitError(f"worktree {workdir} has uncommitted changes")
        git(workdir, "reset", "--hard", f"origin/{branch}")

    def adopt_remote(self, workdir: Path, branch: str) -> None:
        """Move a clean worktree forward to origin/<branch> when another machine pushed more commits; never discard local work."""
        if not self.has_remote() or not git(self.path, "ls-remote", "--heads", "origin", branch, check=False):
            return
        self.fetch(branch)
        local, remote = git(workdir, "rev-parse", "HEAD"), git(workdir, "rev-parse", f"origin/{branch}")
        ancestor = subprocess.run(["git", "-C", str(workdir), "merge-base", "--is-ancestor", local, remote]).returncode == 0
        if local != remote and ancestor and not git(workdir, "status", "--porcelain"):
            git(workdir, "reset", "--hard", remote)

    def commit_subjects(self, workdir: Path, base: str) -> list[str]:
        out = git(workdir, "log", "--no-merges", "--format=%s", f"{base}..HEAD")
        return [line for line in out.splitlines() if line.strip()]

    def cleanup(self, key: str, remote: bool = True) -> None:
        """Remove the ticket worktree and local branch; the remote branch too unless `remote` is false."""
        found = self.find_worktree(key)
        if found:
            self._helper("remove", "--path", str(found[0]), "--delete-branch")
        pushed = self.remote_branch(key) if remote else None
        if pushed:
            git(self.path, "push", "origin", "--delete", pushed, check=False)
