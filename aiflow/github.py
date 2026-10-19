"""Pull requests through the GitHub CLI: open, read CI and feedback, answer threads, update the branch, merge."""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from . import agents

MARKER = "<!-- aiflow -->"
BAD_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
PR_FIELDS = "number,state,url,title,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,mergedAt,statusCheckRollup"
CONVENTIONAL = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)\s]+\))?!?: [A-Z]\S.*[^.]$")

THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){
  reviewThreads(first:100){nodes{id isResolved isOutdated path line comments(first:20){nodes{id body url author{login __typename}}}}}
  reviews(first:100){nodes{id state body author{login __typename}}}
  comments(first:100){nodes{id body createdAt url author{login __typename}}}}}}
"""


class GitHubError(RuntimeError):
    pass


def valid_title(title: str) -> bool:
    """Conventional Commits header as the jooservices repositories enforce it (commitlint and the PR title check)."""
    return 10 <= len(title) <= 120 and bool(CONVENTIONAL.match(title))


def fallback_title(key: str, summary: str, issue_type: str) -> str:
    kind = {"Bug": "fix", "Story": "feat"}.get(issue_type, "chore")
    subject = re.sub(r"\s+", " ", summary).strip().rstrip(".")
    subject = subject[:1].upper() + subject[1:]
    return f"{kind}: {subject[:120 - len(kind) - len(key) - 6]} ({key})"


def with_key(title: str, key: str) -> str:
    return title if f"({key})" in title or len(title) + len(key) + 3 > 120 else f"{title} ({key})"


def checks_state(rollup: list[dict[str, Any]] | None) -> tuple[str, list[dict[str, str]]]:
    """('none'|'pending'|'failed'|'passed', failing checks) from a statusCheckRollup."""
    if not rollup:
        return "none", []
    # A superseded run (title edited, new push) stays in the list as CANCELLED next to its replacement: only the newest run of a check counts.
    newest: dict[str, dict[str, Any]] = {}
    for item in rollup:
        name = item.get("name") or item.get("context") or "?"
        key = f"{item.get('workflowName', '')}/{name}"
        if key not in newest or str(item.get("startedAt") or "") >= str(newest[key].get("startedAt") or ""):
            newest[key] = item
    failed, pending = [], False
    for item in newest.values():
        if item.get("__typename") == "StatusContext":
            state = item.get("state", "")
            name, link = item.get("context", "?"), item.get("targetUrl", "")
            if state in ("FAILURE", "ERROR"):
                failed.append({"name": name, "link": link})
            elif state in ("PENDING", "EXPECTED"):
                pending = True
            continue
        name, link = item.get("name", "?"), item.get("detailsUrl", "")
        if item.get("status") != "COMPLETED":
            pending = True
        elif item.get("conclusion") in BAD_CONCLUSIONS:
            failed.append({"name": name, "link": link})
        elif item.get("conclusion") not in OK_CONCLUSIONS:
            pending = True
    return ("failed" if failed else "pending" if pending else "passed"), failed


def is_bot(author: dict[str, Any] | None) -> bool:
    return bool(author) and (author.get("__typename") == "Bot" or str(author.get("login", "")).endswith("[bot]"))


def parse_feedback(pr: dict[str, Any], seen: set[str], ignore: tuple[str, ...] = ()) -> dict[str, Any]:
    """Split a pull request's GraphQL data into what must be answered.

    items: open review threads, new human comments (accounts in `ignore` are automation, e.g. codecov) and the latest
    'changes requested' review per author, as
    {id, kind, bot, who, where, text}. `stuck` lists human reviews that still request changes after they were sent once."""
    items: list[dict[str, Any]] = []
    stuck: list[str] = []
    for thread in pr["reviewThreads"]["nodes"]:
        if thread["isResolved"] or not thread["comments"]["nodes"]:
            continue
        first = thread["comments"]["nodes"][0]
        where = f"{thread.get('path') or '?'}:{thread.get('line') or '?'}"
        text = "\n".join(f"{(c['author'] or {}).get('login', '?')}: {c['body']}" for c in thread["comments"]["nodes"])
        items.append({"id": f"thread:{thread['id']}", "kind": "thread", "bot": is_bot(first["author"]),
                      "who": (first["author"] or {}).get("login", "?"), "where": where, "text": text[:3000]})
    for comment in pr["comments"]["nodes"]:
        author = comment["author"]
        if is_bot(author) or (author or {}).get("login", "").lower() in ignore or MARKER in comment["body"] or f"comment:{comment['id']}" in seen:
            continue
        items.append({"id": f"comment:{comment['id']}", "kind": "comment", "bot": False, "who": (author or {}).get("login", "?"),
                      "where": "conversation", "text": comment["body"][:3000]})
    latest: dict[str, dict[str, Any]] = {}
    for review in pr["reviews"]["nodes"]:
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(review["author"] or {}).get("login", "?")] = review
    for who, review in latest.items():
        if review["state"] != "CHANGES_REQUESTED":
            continue
        item_id = f"review:{review['id']}"
        if item_id not in seen:
            items.append({"id": item_id, "kind": "review", "bot": is_bot(review["author"]), "who": who,
                          "where": "review", "text": (review["body"] or "(changes requested, no text)")[:3000]})
        elif not is_bot(review["author"]):
            stuck.append(who)
    return {"items": items, "stuck": stuck}


class GitHub:
    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.owner, self.name = slug.split("/", 1)

    def _gh(self, *args: str, timeout: int = 120) -> str:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, env=agents.child_env(), timeout=timeout)
        if result.returncode != 0:
            raise GitHubError((result.stdout + result.stderr).strip()[:600])
        return result.stdout

    # ---- pull requests ---------------------------------------------------------------------------------------
    def find(self, branch: str) -> dict[str, Any] | None:
        data = json.loads(self._gh("pr", "list", "-R", self.slug, "--head", branch, "--state", "all", "--json", PR_FIELDS, "--limit", "5") or "[]")
        data.sort(key=lambda p: {"OPEN": 0, "MERGED": 1}.get(p["state"], 2))
        return data[0] if data else None

    def view(self, number: int) -> dict[str, Any]:
        return json.loads(self._gh("pr", "view", str(number), "-R", self.slug, "--json", PR_FIELDS))

    def create(self, branch: str, base: str, title: str, body: str) -> dict[str, Any]:
        self._gh("pr", "create", "-R", self.slug, "--base", base, "--head", branch, "--title", title, "--body", body)
        found = self.find(branch)
        if not found:
            raise GitHubError("pull request was created but cannot be read back")
        return found

    def update_branch(self, number: int, head_sha: str) -> None:
        self._gh("api", "-X", "PUT", f"repos/{self.slug}/pulls/{number}/update-branch", "-f", f"expected_head_sha={head_sha}")

    def merge(self, number: int, head_sha: str, method: str = "merge") -> None:
        self._gh("pr", "merge", str(number), "-R", self.slug, f"--{method}", "--match-head-commit", head_sha)

    def comment(self, number: int, body: str) -> None:
        self._gh("pr", "comment", str(number), "-R", self.slug, "--body", f"{body}\n\n{MARKER}")

    # ---- feedback --------------------------------------------------------------------------------------------
    def raw_feedback(self, number: int) -> dict[str, Any]:
        out = self._gh("api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"owner={self.owner}", "-F", f"name={self.name}", "-F", f"number={number}")
        return json.loads(out)["data"]["repository"]["pullRequest"]

    def feedback(self, number: int, seen: set[str], ignore: tuple[str, ...] = ()) -> dict[str, Any]:
        return parse_feedback(self.raw_feedback(number), seen, ignore)

    def reply(self, item_id: str, number: int, body: str) -> None:
        kind, _, ref = item_id.partition(":")
        if kind == "thread":
            mutation = "mutation($id:ID!,$body:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}){comment{id}}}"
            self._gh("api", "graphql", "-f", f"query={mutation}", "-F", f"id={ref}", "-f", f"body={body}\n\n{MARKER}")
        else:
            self.comment(number, body)

    def resolve(self, thread_node_id: str) -> None:
        mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
        self._gh("api", "graphql", "-f", f"query={mutation}", "-F", f"id={thread_node_id}")

    # ---- CI --------------------------------------------------------------------------------------------------
    def failure_log(self, link: str, lines: int = 60) -> str:
        """Tail of the failed log for a GitHub Actions job link (best effort)."""
        match = re.search(r"/runs/(\d+)(?:/job/(\d+))?", link or "")
        if not match:
            return ""
        args = ["run", "view", match.group(1), "-R", self.slug, "--log-failed"]
        if match.group(2):
            args += ["--job", match.group(2)]
        try:
            return "\n".join(self._gh(*args, timeout=90).splitlines()[-lines:])[-3500:]
        except (GitHubError, subprocess.TimeoutExpired):
            return ""
