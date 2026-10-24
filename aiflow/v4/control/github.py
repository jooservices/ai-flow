"""GitHub gateway through the GitHub App (jooservices-aiflow-bot).

The control plane holds the App's private key. Nodes never see it: they get a short-lived installation token
narrowed to one repository (contents and pull requests), and only while their fencing token is valid.
"""
from __future__ import annotations

import json
import base64
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

API = "https://api.github.com"
GITHUB_APP_REQUIRED_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "actions": "read",
    "checks": "read",
    "statuses": "read",
    "administration": "write",
}


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitHubApp:
    def __init__(self, app_dir: str, org: str, client: httpx.Client | None = None) -> None:
        d = Path(app_dir)
        self.dir = d
        self.meta: dict[str, Any] = {}
        self.key = ""
        self._fallback_org = org
        self.org = org
        self.c = client or httpx.Client(base_url=API, timeout=30, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"})
        self._inst: int | None = None
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        self._config_stamp: tuple[int, int] | None = None
        self._refresh_config(force=True)

    @property
    def configured(self) -> bool:
        """Whether this gateway still has a complete, currently loaded App configuration."""
        return bool(self._config_stamp is not None and self.meta.get("id") and self.key)

    def _revoke_config(self) -> None:
        """Drop cached credentials when the shared secret files have been removed or become unreadable."""
        with self._lock:
            self.meta = {}
            self.key = ""
            self.org = self._fallback_org
            self._inst = None
            self._cache.clear()
            self._config_stamp = None

    def _refresh_config(self, force: bool = False) -> None:
        """Reload a connection saved by another API replica without restarting workers or control loops."""
        try:
            stamp = ((self.dir / "app.json").stat().st_mtime_ns, (self.dir / "app.pem").stat().st_mtime_ns)
        except OSError:
            self._revoke_config()
            if force:
                raise
            return
        with self._lock:
            if not force and stamp == self._config_stamp:
                return
            import fcntl

            lock_path = self.dir / ".connection.lock"
            lock_fd = os.open(lock_path, os.O_RDONLY) if lock_path.exists() else None
            try:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH)
                stamp = ((self.dir / "app.json").stat().st_mtime_ns,
                         (self.dir / "app.pem").stat().st_mtime_ns)
                meta = json.loads((self.dir / "app.json").read_text(encoding="utf-8"))
                key = (self.dir / "app.pem").read_text(encoding="utf-8")
            except OSError:
                self._revoke_config()
                if force:
                    raise
                return
            finally:
                if lock_fd is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
            if not isinstance(meta, dict) or not meta.get("id") or not key.startswith("-----BEGIN "):
                raise GitHubError("GitHub App configuration is invalid")
            changed = meta != self.meta or key != self.key
            self.meta, self.key = meta, key
            self.org = str(meta.get("account_login") or self.org)
            self._inst = int(meta["installation_id"]) if meta.get("installation_id") else None
            self._config_stamp = stamp
            if changed:
                self._cache.clear()

    def _jwt(self) -> str:
        self._refresh_config()
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.meta["id"])}, self.key, algorithm="RS256")

    def _req(self, method: str, path: str, token: str, body: Any = None, ok=(200, 201, 202, 204)) -> Any:
        r = self.c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code not in ok:
            body_text = r.text[:300]
            raise GitHubError(f"{method} {path} -> {r.status_code}: {body_text}",
                              status_code=r.status_code, response_body=body_text)
        return r.json() if r.content else None

    def installation_id(self) -> int:
        self._refresh_config()
        if self._inst is None:
            endpoint = f"/orgs/{self.org}/installation" if self.org else "/installation"
            self._inst = int(self._req("GET", endpoint, self._jwt())["id"])
        return self._inst

    def app_details(self) -> dict[str, Any]:
        """Read non-secret metadata for the current app using its app JWT."""
        return self._req("GET", "/app", self._jwt())

    def app_installations(self) -> list[dict[str, Any]]:
        """List installations for this app so an owner can select an account after importing it."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            rows = self._req("GET", f"/app/installations?per_page=100&page={page}", self._jwt()) or []
            out.extend(rows)
            if len(rows) < 100:
                return out
            page += 1

    def installation_details(self, installation_id: int) -> dict[str, Any]:
        """Resolve and verify an installation id against this App's credentials."""
        return self._req("GET", f"/app/installations/{int(installation_id)}", self._jwt())

    def token(self, repo: str | None = None, permissions: dict[str, str] | None = None) -> tuple[str, datetime]:
        """Installation token, optionally narrowed to one repo and to the given permissions; cached ~50 min."""
        self._refresh_config()
        perms = permissions or {"contents": "write", "pull_requests": "write", "metadata": "read"}
        cache_key = f"{repo}|{sorted(perms.items())}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit[1] - time.time() > 600:
                return hit[0], datetime.fromtimestamp(hit[1]).astimezone()
            body: dict[str, Any] = {"permissions": perms}
            if repo:
                body["repositories"] = [repo.split("/", 1)[-1]]
            data = self._req("POST", f"/app/installations/{self.installation_id()}/access_tokens", self._jwt(), body)
            exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            self._cache[cache_key] = (data["token"], exp.timestamp())
            return data["token"], exp

    def api(self, method: str, path: str, body: Any = None, repo: str | None = None,
            ok=(200, 201, 202, 204), permissions: dict[str, str] | None = None) -> Any:
        default = {"contents": "write", "pull_requests": "write", "metadata": "read",
                   "checks": "read", "statuses": "read"} if repo else None
        tok, _ = self.token(repo, permissions if permissions is not None else default)
        return self._req(method, path, tok, body, ok)

    # --- pull requests --------------------------------------------------------------------------------------
    def find_pr(self, repo: str, head: str) -> dict | None:
        prs = self.api("GET", f"/repos/{self.org}/{repo}/pulls?state=open&head={self.org}:{head}", repo=repo)
        return prs[0] if prs else None

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool = True) -> dict:
        existing = self.find_pr(repo, head)
        if existing:
            if existing.get("title") != title:
                return self.api("PATCH", f"/repos/{self.org}/{repo}/pulls/{existing['number']}",
                                {"title": title}, repo=repo)
            return existing
        return self.api("POST", f"/repos/{self.org}/{repo}/pulls",
                        {"title": title, "head": head, "base": base, "body": body, "draft": draft}, repo=repo)

    def release_pr(self, repo: str, head: str, base: str) -> dict | None:
        """Find an open or merged release PR by its exact head and base branches."""
        prs = self.api("GET", f"/repos/{self.org}/{repo}/pulls?state=all&head={self.org}:{head}&base={base}",
                       repo=repo)
        return next((pr for pr in prs if (pr.get("head") or {}).get("ref") == head
                     and (pr.get("base") or {}).get("ref") == base), None)

    def update_pr_body(self, repo: str, number: int, body: str) -> dict:
        """Refresh a release PR's ticket notes after its Jira version membership changes."""
        return self.api("PATCH", f"/repos/{self.org}/{repo}/pulls/{number}",
                        {"body": body[:60000]}, repo=repo)

    def pr(self, repo: str, number: int) -> dict:
        return self.api("GET", f"/repos/{self.org}/{repo}/pulls/{number}", repo=repo)

    def reopen_pr(self, repo: str, number: int) -> dict:
        """Reopen the existing release PR after a prior merge attempt was blocked by repository policy."""
        return self.api("PATCH", f"/repos/{self.org}/{repo}/pulls/{number}", {"state": "open"}, repo=repo)

    def close_pr(self, repo: str, number: int) -> dict:
        """Close a pull request rejected in human review without merging it."""
        return self.api("PATCH", f"/repos/{self.org}/{repo}/pulls/{number}", {"state": "closed"}, repo=repo)

    def comment(self, repo: str, number: int, body: str) -> None:
        self.api("POST", f"/repos/{self.org}/{repo}/issues/{number}/comments", {"body": body[:60000]}, repo=repo)

    # --- control repo knowledge and policy files -----------------------------------------------------------
    def default_branch(self, repo: str) -> str:
        data = self.api("GET", f"/repos/{self.org}/{repo}", repo=repo)
        return str(data["default_branch"])

    def installation_repositories(self) -> list[dict[str, str]]:
        """List repositories this GitHub App installation can access."""
        self._refresh_config()
        out: list[dict[str, str]] = []
        page = 1
        while True:
            data = self.api("GET", f"/installation/repositories?per_page=100&page={page}")
            repositories = data.get("repositories", [])
            out.extend({"full_name": str(item.get("full_name") or ""),
                        "name": str(item.get("name") or ""),
                        "default_branch": str(item.get("default_branch") or "")}
                       for item in repositories if item.get("full_name"))
            if len(repositories) < 100:
                return out
            page += 1

    def branch_exists(self, repo: str, branch: str) -> bool:
        """Check a branch name without accepting an arbitrary repository from the caller."""
        encoded_branch = quote(branch, safe="")
        try:
            self.api("GET", f"/repos/{self.org}/{repo}/branches/{encoded_branch}", repo=repo)
        except GitHubError as exc:
            if "-> 404:" in str(exc):
                return False
            raise
        return True

    def create_branch(self, repo: str, branch: str, base: str) -> None:
        ref = quote(f"heads/{base}", safe="/")
        data = self.api("GET", f"/repos/{self.org}/{repo}/git/ref/{ref}", repo=repo)
        self.api("POST", f"/repos/{self.org}/{repo}/git/refs",
                 {"ref": f"refs/heads/{branch}", "sha": data["object"]["sha"]}, repo=repo)

    def delete_branch(self, repo: str, branch: str) -> bool:
        """Delete one remote branch; a branch that is already gone counts as cleaned up."""
        ref = quote(f"heads/{branch}", safe="/")
        try:
            self.api("DELETE", f"/repos/{self.org}/{repo}/git/refs/{ref}", repo=repo)
        except GitHubError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def create_branch_at_sha(self, repo: str, branch: str, sha: str) -> None:
        """Restore a deleted release branch at the exact audited commit for a post-merge retry."""
        self.api("POST", f"/repos/{self.org}/{repo}/git/refs",
                 {"ref": f"refs/heads/{branch}", "sha": sha}, repo=repo)

    def commit_file(self, repo: str, branch: str, path: str, content: str, message: str) -> str:
        """Write one UTF-8 file through the GitHub App; GitHub records the installation bot as committer."""
        encoded_path = quote(path, safe="/")
        branch_q = quote(branch, safe="")
        sha = ""
        try:
            current = self.api("GET", f"/repos/{self.org}/{repo}/contents/{encoded_path}?ref={branch_q}", repo=repo)
            sha = str(current.get("sha") or "")
        except GitHubError as e:
            if "-> 404:" not in str(e):
                raise
        body: dict[str, Any] = {"message": message[:200], "content": base64.b64encode(content.encode()).decode(),
                                "branch": branch}
        if sha:
            body["sha"] = sha
        result = self.api("PUT", f"/repos/{self.org}/{repo}/contents/{encoded_path}", body, repo=repo)
        return str((result.get("commit") or {}).get("sha") or "")

    def file_content(self, repo: str, branch: str, path: str) -> str | None:
        """Read a small UTF-8 repository file from one branch; a missing file returns None."""
        encoded_path = quote(path, safe="/")
        branch_q = quote(branch, safe="")
        try:
            item = self.api("GET", f"/repos/{self.org}/{repo}/contents/{encoded_path}?ref={branch_q}", repo=repo)
        except GitHubError as exc:
            if "-> 404:" in str(exc):
                return None
            raise
        if item.get("type") != "file" or not item.get("content"):
            return None
        return base64.b64decode(item["content"]).decode("utf-8")

    def repository_files(self, repo: str, ref: str) -> list[str]:
        """List paths in a branch tree so version-bearing manifests can be found recursively."""
        sha = self.branch_sha(repo, ref)
        tree = self.api("GET", f"/repos/{self.org}/{repo}/git/trees/{sha}?recursive=1", repo=repo)
        return [str(row["path"]) for row in tree.get("tree", [])
                if row.get("type") == "blob" and row.get("path")]

    def delete_file(self, repo: str, branch: str, path: str, message: str) -> str:
        """Delete one control-repo file through the App, recording the removal as a bot commit."""
        encoded_path = quote(path, safe="/")
        branch_q = quote(branch, safe="")
        current = self.api("GET", f"/repos/{self.org}/{repo}/contents/{encoded_path}?ref={branch_q}", repo=repo)
        result = self.api("DELETE", f"/repos/{self.org}/{repo}/contents/{encoded_path}",
                          {"message": message[:200], "sha": str(current["sha"]), "branch": branch}, repo=repo)
        return str((result.get("commit") or {}).get("sha") or "")

    def create_control_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        return self.create_pr(repo, head, base, title, body, draft=True)

    def ready_for_review(self, repo: str, number: int) -> None:
        """Draft PRs cannot be merged; REST has no un-draft call, GraphQL does."""
        pr = self.pr(repo, number)
        if not pr.get("draft"):
            return
        tok, _ = self.token(repo, {"contents": "write", "pull_requests": "write", "metadata": "read"})
        q = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
        data = self._req("POST", "/graphql", tok, {"query": q, "variables": {"id": pr["node_id"]}})
        if data and data.get("errors"):
            raise GitHubError(f"ready for review: {data['errors']}")

    def merge_pr(self, repo: str, number: int, title: str = "", method: str = "squash", sha: str = "") -> dict:
        """Mark ready and merge; returns {merged, sha, message}. A refused merge (conflict, stale head) is not raised.

        `sha` pins the merge to the commit whose checks were read: GitHub refuses (409) when the branch moved on."""
        self.ready_for_review(repo, number)
        body: dict[str, Any] = {"merge_method": method}
        if sha:
            body["sha"] = sha
        if title:
            body["commit_title"] = f"{title} (#{number})"[:250]
        r = self.c.request("PUT", f"/repos/{self.org}/{repo}/pulls/{number}/merge", json=body, headers={
            "Authorization": f"Bearer {self.token(repo, {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'})[0]}"})
        data = r.json() if r.content else {}
        if r.status_code == 200:
            return {"merged": True, "sha": data.get("sha", ""), "message": data.get("message", "")}
        pr = self.pr(repo, number)
        return {"merged": False, "sha": "", "message": f"{r.status_code}: {data.get('message', r.text[:200])}",
                "mergeable_state": pr.get("mergeable_state", ""), "head_sha": pr["head"]["sha"]}

    def update_branch(self, repo: str, number: int) -> dict:
        """Merge the base into the PR branch on GitHub (needed when the base requires up-to-date branches)."""
        r = self.c.request("PUT", f"/repos/{self.org}/{repo}/pulls/{number}/update-branch", json={}, headers={
            "Authorization": f"Bearer {self.token(repo, {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read', 'workflows': 'write'})[0]}"})
        data = r.json() if r.content else {}
        return {"ok": r.status_code in (200, 202), "message": f"{r.status_code}: {data.get('message', '')}"}

    def checks(self, repo: str, sha: str) -> list[dict]:
        """Check runs and commit statuses of a commit: name, state (pending|success|failure|neutral), url."""
        out = []
        runs = self.api("GET", f"/repos/{self.org}/{repo}/commits/{sha}/check-runs?per_page=100", repo=repo)
        for c in runs.get("check_runs", []):
            state = "pending" if c["status"] != "completed" else (
                "success" if c["conclusion"] in ("success", "skipped") else "neutral" if c["conclusion"] == "neutral" else "failure")
            out.append({"name": c["name"], "state": state, "url": c.get("html_url", "")})
        st = self.api("GET", f"/repos/{self.org}/{repo}/commits/{sha}/status", repo=repo)
        for c in st.get("statuses", []):
            state = {"success": "success", "pending": "pending"}.get(c["state"], "failure")
            out.append({"name": c["context"], "state": state, "url": c.get("target_url", "")})
        return out

    def failing_job_log(self, repo: str, sha: str, lines: int = 200) -> dict:
        """The tail of the log of the first failing GitHub Actions job of a commit (AC6 of JAIF-255): a developer
        never has to ask for a CI log. Returns the job name, its URL and at most `lines` lines (20,000 characters)."""
        lines = max(1, min(int(lines), 1000))
        perms = {"actions": "read", "metadata": "read"}
        base = f"/repos/{self.org}/{repo}"
        runs = self.api("GET", f"{base}/actions/runs?head_sha={quote(sha, safe='')}&per_page=20", repo=repo,
                        permissions=perms) or {}
        for run in runs.get("workflow_runs", []):
            if run.get("status") == "completed" and run.get("conclusion") in ("success", "skipped", "neutral"):
                continue
            jobs = self.api("GET", f"{base}/actions/runs/{run['id']}/jobs?per_page=100", repo=repo,
                            permissions=perms) or {}
            for job in jobs.get("jobs", []):
                if job.get("conclusion") not in ("failure", "timed_out", "cancelled"):
                    continue
                token, _ = self.token(repo, perms)
                response = self.c.request("GET", f"{base}/actions/jobs/{job['id']}/logs", follow_redirects=True,
                                          headers={"Authorization": f"Bearer {token}"})
                if response.status_code != 200:
                    raise GitHubError(f"GET job log -> {response.status_code}: {response.text[:200]}")
                text = re.sub(r"\x1b\[[0-9;]*m", "", response.text)
                text = re.sub(r"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", text)
                tail = "\n".join(text.splitlines()[-lines:])[-20_000:]
                failed_steps = [s.get("name", "") for s in job.get("steps", []) if s.get("conclusion") == "failure"]
                return {"found": True, "job": job.get("name", ""), "workflow": run.get("name", ""),
                        "conclusion": job.get("conclusion", ""), "url": job.get("html_url", ""),
                        "failed_steps": failed_steps, "lines": len(tail.splitlines()), "tail": tail}
        return {"found": False, "job": "", "tail": "", "message": "no failing GitHub Actions job for this commit"}

    # --- project version releases ---------------------------------------------------------------------------
    def branch_sha(self, repo: str, branch: str) -> str:
        """Return the current head SHA for a configured branch."""
        data = self.api("GET", f"/repos/{self.org}/{repo}/branches/{quote(branch, safe='')}", repo=repo)
        return str(data["commit"]["sha"])

    def merge_branch(self, repo: str, base: str, head: str, message: str) -> dict:
        """Merge one configured branch into another through GitHub's merge API."""
        return self.api("POST", f"/repos/{self.org}/{repo}/merges",
                        {"base": base, "head": head, "commit_message": message[:500]}, repo=repo,
                        ok=(200, 201, 204)) or {"sha": "", "merged": False}

    def tag_sha(self, repo: str, tag: str) -> str | None:
        """Return a tag ref's target commit, resolving annotated tags when needed."""
        path = f"/repos/{self.org}/{repo}/git/ref/tags/{quote(tag, safe='/')}"
        try:
            ref = self.api("GET", path, repo=repo)
        except GitHubError as exc:
            if "-> 404:" in str(exc):
                return None
            raise
        obj = ref["object"]
        if obj.get("type") == "tag":
            obj = self.api("GET", f"/repos/{self.org}/{repo}/git/tags/{obj['sha']}", repo=repo)["object"]
        return str(obj.get("sha") or "")

    def create_tag(self, repo: str, tag: str, sha: str) -> None:
        """Create a lightweight version tag on an exact commit."""
        self.api("POST", f"/repos/{self.org}/{repo}/git/refs",
                 {"ref": f"refs/tags/{tag}", "sha": sha}, repo=repo, ok=(201,))

    def release_by_tag(self, repo: str, tag: str) -> dict | None:
        path = f"/repos/{self.org}/{repo}/releases/tags/{quote(tag, safe='')}"
        try:
            return self.api("GET", path, repo=repo)
        except GitHubError as exc:
            if "-> 404:" in str(exc):
                return None
            raise

    def create_release(self, repo: str, tag: str, name: str, notes: str) -> dict:
        return self.api("POST", f"/repos/{self.org}/{repo}/releases",
                        {"tag_name": tag, "name": name, "body": notes, "draft": False,
                         "prerelease": "-alpha" in name or "-beta" in name}, repo=repo, ok=(201,),
                        permissions={"contents": "write", "metadata": "read", "workflows": "write"})

    def compare_branches(self, repo: str, base: str, head: str) -> dict:
        """Return GitHub's comparison for `head` relative to `base` (including `ahead_by`)."""
        return self.api("GET", f"/repos/{self.org}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
                        repo=repo)

    def enable_merge_commit(self, repo: str) -> None:
        """Allow merge commits for release PRs when repository settings currently permit only squash/rebase."""
        settings = self.api("GET", f"/repos/{self.org}/{repo}", repo=repo)
        if settings.get("allow_merge_commit") is True:
            return
        self.api("PATCH", f"/repos/{self.org}/{repo}", {"allow_merge_commit": True}, repo=repo,
                 permissions={"administration": "write", "metadata": "read"})

    def bot_identity(self) -> tuple[str, str]:
        slug = self.meta["slug"]
        user = self._req("GET", f"/users/{slug}%5Bbot%5D", self.token()[0])
        return f"{slug}[bot]", f"{user['id']}+{slug}[bot]@users.noreply.github.com"
