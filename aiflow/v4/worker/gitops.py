"""Git for workers: one shared bare mirror per repo, one worktree per ticket, push with a short-lived token.

Git runs as the AI user (aiflow-worker) so the worktree stays owned by it. The token reaches git through
GIT_ASKPASS and the environment of that one process; it is never written to disk or into a remote URL.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ASKPASS = "#!/bin/sh\ncase \"$1\" in *sername*) echo x-access-token ;; *) echo \"$AIFLOW4_GIT_TOKEN\" ;; esac\n"


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, srv_root: str, run_as: list[str] | None = None, askpass_dir: str | None = None) -> None:
        self.root = Path(srv_root)
        self.run_as = run_as or []
        self.askpass = Path(askpass_dir or (self.root / "cache")) / "aiflow4-askpass.sh"

    def mkdirs(self, path: Path) -> None:
        """Create path and its missing parents owned by the AI user (the worker runs as root, git does not)."""
        missing = []
        cur = path
        while not cur.exists():
            missing.append(cur)
            cur = cur.parent
        for d in reversed(missing):
            d.mkdir(exist_ok=True)
            if self.run_as:
                try:
                    shutil.chown(d, "aiflow-worker", "aiflow-worker")
                except (LookupError, PermissionError):
                    pass

    def _ensure_askpass(self) -> None:
        if not self.askpass.exists():
            self.askpass.parent.mkdir(parents=True, exist_ok=True)
            self.askpass.write_text(ASKPASS)
            self.askpass.chmod(0o755)

    def run(self, *args: str, cwd: str | Path | None = None, token: str = "", check: bool = True,
            identity: tuple[str, str] | None = None) -> str:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.root / "cache"), "LANG": "C.UTF-8",
               "GIT_TERMINAL_PROMPT": "0"}
        if token:
            self._ensure_askpass()
            env["GIT_ASKPASS"] = str(self.askpass)
            env["AIFLOW4_GIT_TOKEN"] = token
        if identity:
            env.update({"GIT_AUTHOR_NAME": identity[0], "GIT_AUTHOR_EMAIL": identity[1],
                        "GIT_COMMITTER_NAME": identity[0], "GIT_COMMITTER_EMAIL": identity[1]})
        p = subprocess.run(self.run_as + ["git", *args], cwd=str(cwd) if cwd else None, env=env,
                           capture_output=True, text=True, timeout=600)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])}: {p.stderr.strip()[-400:]}")
        return p.stdout

    def mirror(self, repo: str, token: str) -> Path:
        """Bare clone with remote-tracking refs, fetched before every use."""
        path = self.root / "mirrors" / f"{repo.replace('/', '__')}.git"
        url = f"https://github.com/{repo}.git"
        if not (path / "HEAD").exists():
            self.mkdirs(path.parent)
            self.run("clone", "--bare", "--quiet", url, str(path), token=token)
            self.run("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=path)
        self.run("fetch", "--prune", "--quiet", "origin", cwd=path, token=token)
        return path

    def remote_branch_exists(self, mirror: Path, branch: str) -> bool:
        return bool(self.run("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", cwd=mirror, check=False).strip())

    def worktree(self, mirror: Path, path: Path, branch: str, base: str) -> str:
        """Worktree for the ticket: continue the ticket's branch if it exists remotely, else start from base."""
        self.run("worktree", "prune", cwd=mirror, check=False)
        start = f"origin/{branch}" if self.remote_branch_exists(mirror, branch) else f"origin/{base}"
        if path.exists() and (path / ".git").exists():
            self.run("fetch", "--quiet", cwd=path, check=False)
            self.scrub(path)
            self.run("checkout", "-q", "-B", branch, start, cwd=path)
            self.run("reset", "-q", "--hard", start, cwd=path)
            self.run("clean", "-qfdx", "-e", "vendor/", "-e", "node_modules/", cwd=path)
        else:
            self.mkdirs(path.parent)
            self.run("worktree", "add", "-q", "-B", branch, str(path), start, cwd=mirror)
        return start

    def scrub(self, path: Path) -> None:
        """Drop whatever a previous run left in a reused worktree: an unfinished merge, edits, untracked files."""
        self.run("merge", "--abort", cwd=path, check=False)
        self.run("reset", "-q", "--hard", cwd=path, check=False)
        self.run("clean", "-qfdx", "-e", "vendor/", "-e", "node_modules/", cwd=path, check=False)

    def detached(self, mirror: Path, path: Path, ref: str) -> str:
        """Worktree at a commit without a branch (verify reads the PR head; the dev worktree may hold the branch)."""
        self.run("worktree", "prune", cwd=mirror, check=False)
        if path.exists() and (path / ".git").exists():
            self.scrub(path)
            self.run("checkout", "-q", "--detach", ref, cwd=path)
            self.run("reset", "-q", "--hard", ref, cwd=path)
            self.run("clean", "-qfdx", "-e", "vendor/", "-e", "node_modules/", cwd=path)
        else:
            self.mkdirs(path.parent)
            self.run("worktree", "add", "-q", "--detach", str(path), ref, cwd=mirror)
        return self.run("rev-parse", "HEAD", cwd=path).strip()

    def readonly_worktree(self, mirror: Path, path: Path, ref: str) -> str:
        """Create a detached checkout owned by root and readable, but not writable, by the AI user."""
        self.run("worktree", "prune", "--expire=now", cwd=mirror, check=False)
        self.mkdirs(path.parent)
        self.run("worktree", "add", "-q", "--detach", str(path), ref, cwd=mirror)
        # Codex's sandbox protects <cwd>/.codex and <cwd>/.agents and creates them when missing, which fails in a
        # read-only checkout ("Can't mkdir .agents"): create them before the tree becomes read-only (git ignores empty dirs)
        for name in (".codex", ".agents"):
            (path / name).mkdir(exist_ok=True)
        for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
            for name in dirs + files:
                item = Path(root) / name
                if item.is_symlink():
                    continue
                mode = item.stat().st_mode
                if os.geteuid() == 0:
                    os.chown(item, 0, 0)
                os.chmod(item, 0o555 if item.is_dir() or mode & 0o111 else 0o444)
        if os.geteuid() == 0:
            os.chown(path, 0, 0)
        os.chmod(path, 0o555)
        # root owns the checkout and git runs as the AI user: without safe.directory git refuses it ("dubious ownership")
        return self.run("-c", f"safe.directory={path}", "rev-parse", "HEAD", cwd=path).strip()

    def remove_worktree(self, mirror: Path, path: Path) -> None:
        """Remove a triage checkout after the AI process has stopped, then prune its Git metadata."""
        if path.exists():
            for root, _dirs, _files in os.walk(path, topdown=True, followlinks=False):
                os.chmod(root, 0o700)
            shutil.rmtree(path)
        self.run("worktree", "prune", "--expire=now", cwd=mirror, check=False)

    def merge_base_in(self, path: Path, base: str, identity: tuple[str, str], commit: bool = True) -> list[str]:
        """Merge origin/<base> into the checked-out branch. Returns the conflicted files ([] when clean). With
        conflicts the merge stays in progress so the AI can resolve the markers and the worker commits it."""
        behind = self.run("rev-list", "--count", f"HEAD..origin/{base}", cwd=path, check=False).strip()
        if behind in ("", "0"):
            return []
        args = ["merge", "--no-edit", "-q"] + ([] if commit else ["--no-commit", "--no-ff"]) + [f"origin/{base}"]
        self.run(*args, cwd=path, check=False, identity=identity)
        out = self.run("diff", "--name-only", "--diff-filter=U", cwd=path, check=False)
        return [x for x in out.splitlines() if x.strip()]

    def abort_merge(self, path: Path) -> None:
        self.run("merge", "--abort", cwd=path, check=False)

    def changed(self, path: Path) -> list[str]:
        out = self.run("status", "--porcelain", "--untracked-files=all", cwd=path)
        return [line[3:] for line in out.splitlines() if line.strip()]

    def commit_all(self, path: Path, message: str, identity: tuple[str, str]) -> str:
        self.run("add", "-A", cwd=path)
        p = subprocess.run(self.run_as + ["git", "commit", "-q", "-F", "-"], cwd=str(path), input=message, text=True,
                           capture_output=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                     "HOME": str(self.root / "cache"),
                                                     "GIT_AUTHOR_NAME": identity[0], "GIT_AUTHOR_EMAIL": identity[1],
                                                     "GIT_COMMITTER_NAME": identity[0], "GIT_COMMITTER_EMAIL": identity[1]})
        if p.returncode != 0:
            raise GitError(f"git commit: {p.stderr.strip()[-400:]}")
        return self.run("rev-parse", "HEAD", cwd=path).strip()

    def push(self, path: Path, branch: str, token: str) -> None:
        self.run("push", "--quiet", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}", cwd=path, token=token)
