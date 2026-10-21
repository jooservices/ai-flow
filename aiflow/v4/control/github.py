"""GitHub gateway through the GitHub App (jooservices-aiflow-bot).

The control plane holds the App's private key. Nodes never see it: they get a short-lived installation token
narrowed to one repository (contents and pull requests), and only while their fencing token is valid.
"""
from __future__ import annotations

import json
import base64
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubApp:
    def __init__(self, app_dir: str, org: str, client: httpx.Client | None = None) -> None:
        d = Path(app_dir)
        self.meta = json.loads((d / "app.json").read_text())
        self.key = (d / "app.pem").read_text()
        self.org = org
        self.c = client or httpx.Client(base_url=API, timeout=30, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"})
        self._inst: int | None = None
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.meta["id"])}, self.key, algorithm="RS256")

    def _req(self, method: str, path: str, token: str, body: Any = None, ok=(200, 201, 202, 204)) -> Any:
        r = self.c.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code not in ok:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def installation_id(self) -> int:
        if self._inst is None:
            self._inst = int(self._req("GET", f"/orgs/{self.org}/installation", self._jwt())["id"])
        return self._inst

    def token(self, repo: str | None = None, permissions: dict[str, str] | None = None) -> tuple[str, datetime]:
        """Installation token, optionally narrowed to one repo and to the given permissions; cached ~50 min."""
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

    def api(self, method: str, path: str, body: Any = None, repo: str | None = None, ok=(200, 201, 202, 204)) -> Any:
        tok, _ = self.token(repo, {"contents": "write", "pull_requests": "write", "metadata": "read",
                                   "checks": "read", "statuses": "read"} if repo else None)
        return self._req(method, path, tok, body, ok)

    # --- pull requests --------------------------------------------------------------------------------------
    def find_pr(self, repo: str, head: str) -> dict | None:
        prs = self.api("GET", f"/repos/{self.org}/{repo}/pulls?state=open&head={self.org}:{head}", repo=repo)
        return prs[0] if prs else None

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool = True) -> dict:
        existing = self.find_pr(repo, head)
        if existing:
            return existing
        return self.api("POST", f"/repos/{self.org}/{repo}/pulls",
                        {"title": title, "head": head, "base": base, "body": body, "draft": draft}, repo=repo)

    def pr(self, repo: str, number: int) -> dict:
        return self.api("GET", f"/repos/{self.org}/{repo}/pulls/{number}", repo=repo)

    def comment(self, repo: str, number: int, body: str) -> None:
        self.api("POST", f"/repos/{self.org}/{repo}/issues/{number}/comments", {"body": body[:60000]}, repo=repo)

    # --- control repo knowledge and policy files -----------------------------------------------------------
    def default_branch(self, repo: str) -> str:
        data = self.api("GET", f"/repos/{self.org}/{repo}", repo=repo)
        return str(data["default_branch"])

    def installation_repositories(self) -> list[dict[str, str]]:
        """List repositories this GitHub App installation can access."""
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

    def merge_pr(self, repo: str, number: int, title: str = "", method: str = "squash") -> dict:
        """Mark ready and merge; returns {merged, sha, message}. A refused merge (conflict, stale head) is not raised."""
        self.ready_for_review(repo, number)
        body: dict[str, Any] = {"merge_method": method}
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

    def bot_identity(self) -> tuple[str, str]:
        slug = self.meta["slug"]
        user = self._req("GET", f"/users/{slug}%5Bbot%5D", self.token()[0])
        return f"{slug}[bot]", f"{user['id']}+{slug}[bot]@users.noreply.github.com"
