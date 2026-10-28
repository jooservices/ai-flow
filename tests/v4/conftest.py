"""Fixtures: a real Redis and PostgreSQL (AIFLOW4_TEST_REDIS_URL / AIFLOW4_TEST_DATABASE_URL), a control repo on
disk, and fakes for Jira and GitHub so API flows run without the network."""
from __future__ import annotations

import os
import threading
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import redis

from aiflow.v4.control import api, system_check_api
from aiflow.v4.control.api import Ctx, create_app
from aiflow.v4.control.db import DB, J
from aiflow.v4.control.events import Events
from aiflow.v4.control.jira import Jira
from aiflow.v4.control import licensing
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.core.fencing import Leases
from aiflow.v4.core.settings import Settings
from aiflow.v4.core import secretbox

REDIS_URL = os.environ.get("AIFLOW4_TEST_REDIS_URL", "")
DATABASE_URL = os.environ.get("AIFLOW4_TEST_DATABASE_URL", "")
REDIS_PREFIX = os.environ.get("AIFLOW4_TEST_REDIS_PREFIX", os.environ.get("REDIS_PREFIX", "aiflow4_test:"))


@pytest.fixture(autouse=True)
def runtime_operation_key_in_test_workspace(tmp_path, monkeypatch):
    """Keep per-node runtime-setting private keys inside the test's temporary workspace."""
    monkeypatch.setenv("AIFLOW4_NODE_OPERATION_KEY_FILE", str(tmp_path / "runtime-settings.key"))


def pytest_configure(config):
    config.addinivalue_line("markers", "raw_csrf: do not add the CSRF header to console requests (tests of the protection)")
    # The CI unit-test job does not provision a production secret key. Give its isolated process test-only key material
    # so app contexts can initialize; security tests explicitly remove or replace it when exercising fail-closed paths.
    os.environ["AIFLOW4_SECRET_KEY"] = secretbox.generate_key()


@pytest.fixture(autouse=True)
def console_csrf_header(request, monkeypatch):
    """A console session cookie needs the session's X-CSRF-Token on every state-changing request (AC4 of JAIF-231).
    Tests of other features sign in with a forged cookie, so this adds the matching token for them. Tests of the
    protection itself opt out with `@pytest.mark.raw_csrf` and send exactly what they mean to."""
    raw_csrf = request.node.get_closest_marker("raw_csrf") is not None
    from starlette.testclient import TestClient
    from aiflow.v4.control.console_auth import COOKIE_NAME, csrf_token
    original = TestClient.request

    def request_with_token(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        if (method.upper() == "POST" and str(url).split("?", 1)[0].endswith("/v1/auth/login")
                and not any(k.lower() in {"origin", "referer"} for k in headers)):
            headers = {**headers, "Origin": str(self.base_url).rstrip("/")}
            kwargs["headers"] = headers
        cookie = ([c.value for c in self.cookies.jar if c.name == COOKIE_NAME] or [None])[-1]
        if (not raw_csrf and cookie and method.upper() not in ("GET", "HEAD", "OPTIONS")
                and not any(k.lower() == "x-csrf-token" for k in headers)):
            ctx = getattr(self.app, "state", None) and getattr(self.app.state, "ctx", None)
            if ctx is not None:
                kwargs["headers"] = {**headers, "X-CSRF-Token": csrf_token(cookie, ctx.settings.console_session_secret)}
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", request_with_token)


@pytest.fixture
def leases():
    if not REDIS_URL:
        pytest.fail("AIFLOW4_TEST_REDIS_URL must point at an isolated test Redis database")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        redis_db = urlsplit(REDIS_URL).path.strip("/")
        if redis_db in {"", "0", "5"} or not (
                REDIS_PREFIX.endswith("_test:") or re.search(r"_wt[a-z0-9]+:$", REDIS_PREFIX)):
            pytest.fail("local tests require a non-live Redis index and a *_test: or *_wt<id>: key prefix")
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    prefix = f"{REDIS_PREFIX}t{uuid.uuid4().hex[:8]}:"
    yield Leases(r, prefix)
    for k in r.scan_iter(match=prefix + "*"):
        r.delete(k)


@pytest.fixture(scope="session")
def test_database_url():
    """Use only the database supplied by the test runner; never create, sweep or drop databases here."""
    if not DATABASE_URL:
        pytest.fail("AIFLOW4_TEST_DATABASE_URL must point at a dedicated test or worktree database")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        from psycopg.conninfo import conninfo_to_dict

        name = str(conninfo_to_dict(DATABASE_URL).get("dbname") or urlsplit(DATABASE_URL).path.lstrip("/"))
        if not (name.endswith("_test") or re.search(r"_wt[a-z0-9]+$", name)):
            pytest.fail("local tests require a dedicated *_test or *_wt<id> database")
    yield DATABASE_URL

@pytest.fixture
def db(test_database_url):
    d = DB(test_database_url)
    with d.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    d.migrate()
    yield d
    d.close()


@pytest.fixture
def control_repo(tmp_path: Path) -> Path:
    r = tmp_path / "aiflow-control"
    (r / "nodes").mkdir(parents=True)
    (r / "projects").mkdir()
    (r / "policy").mkdir()
    (r / "versions.lock").write_text("schema: 1\ntools: {claude_code: 2.1.305}\n")
    (r / "policy" / "default.yaml").write_text(
        "version: 1\ncommands:\n  allow: ['composer *', 'npm *', 'vendor/bin/*', 'mkdir *']\n  deny: ['git push*', 'git commit*', 'sudo *']\n"
        "files:\n  write_within: worktree\n  deny_read: ['/etc/aiflow/secrets/**']\n")
    (r / "profiles.yaml").write_text(
        "providers:\n  claude: {models: {senior: opus, middle: sonnet}}\n  codex: {models: {middle: gpt-6-luna}, effort: {middle: high}}\n")
    (r / "accounts.yaml").write_text(
        "accounts:\n  - {id: claude-01, provider: claude, node: cc, tier: senior, roles: [dev, verify, master]}\n"
        "  - {id: codex-01, provider: codex, node: node1, tier: middle, roles: [dev, verify, master]}\n"
        "  - {id: opencode-01, provider: opencode, node: cc, tier: middle, roles: [dev], explicit_only: true}\n")
    (r / "nodes" / "cc.yaml").write_text("name: cc\nip: 10.44.0.10\nslots: 2\nprojects: [TESTWP]\n")
    (r / "nodes" / "node1.yaml").write_text("name: node1\nip: 10.44.0.11\nslots: 2\nprojects: [TESTWP]\n")
    (r / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nbase: develop\nchecks: [composer install, vendor/bin/phpunit]\n"
        "limits: {work_minutes: {target: 30, warn: 45, hard: 60}}\n")
    return r


class FakeJira:
    normalize = staticmethod(Jira.normalize)

    def __init__(self) -> None:
        self.status_of: dict[str, str] = {}
        self.comments: list[tuple[str, str]] = []
        self.recent_comment_data: dict[str, list[dict]] = {}
        self.jira_account_id = "jira-owner-account"
        self.fields: list[tuple[str, dict]] = []
        self.labels: list[tuple[str, str, str]] = []
        self.attachments: list[tuple[str, str, bytes, str]] = []
        self.projects_data = [{"key": "TESTWP", "name": "Test WordPress"},
                              {"key": "NEWAPP", "name": "New App"}]
        self.project_workflows: dict[str, list[str]] = {}
        self.project_reads: list[str] = []
        self.project_permission_reads: list[str] = []
        self.project_permissions: dict[str, dict[str, bool]] = {}
        self.versions_data: dict[str, list[dict]] = {}
        self.version_reads: list[str] = []
        self.search_data: list[dict] = []
        self.search_queries: list[str] = []
        self.deleted: list[str] = []
        self.transitions: list[tuple[str, str]] = []
        self.version_data: dict[str, list[dict]] = {}
        self.version_issue_data: dict[tuple[str, str], list[dict]] = {}
        self.released_versions: list[str] = []
        self.version_release_dates: dict[str, str] = {}
        self.issue_links: list[tuple[str, str, str]] = []

    def projects(self) -> list[dict[str, str]]:
        return list(self.projects_data)

    def project_statuses(self, key: str) -> list[str]:
        self.project_reads.append(key)
        return self.project_workflows.get(key, ["Backlog", "Intake", "To Do", "In Progress", "In Verify",
                                                "Needs owner", "Done"])

    def project_write_permissions(self, key: str) -> dict[str, bool]:
        self.project_permission_reads.append(key)
        return self.project_permissions.get(key, {
            "BROWSE_PROJECTS": True, "CREATE_ISSUES": True, "EDIT_ISSUES": True, "LINK_ISSUES": True,
            "TRANSITION_ISSUES": True, "ADD_COMMENTS": True,
        })

    def project_versions(self, key: str) -> list[dict]:
        self.version_reads.append(key)
        return list(self.versions_data.get(key, []))

    def search(self, jql: str):
        self.search_queries.append(jql)
        return iter(self.search_data)

    def search_history(self, jql: str):
        return self.search(jql)

    def versions(self, project: str) -> list[dict]:
        return list(self.version_data.get(project, []))

    def version_issues(self, project: str, version_id: str) -> list[dict]:
        return list(self.version_issue_data.get((project, str(version_id)), []))

    def release_version(self, version_id: str, released: bool = True) -> None:
        from datetime import date

        self.released_versions.append(str(version_id))
        if released:
            self.version_release_dates[str(version_id)] = date.today().isoformat()
        for versions in self.version_data.values():
            for version in versions:
                if str(version["id"]) == str(version_id):
                    version["released"] = released
                    if released:
                        version["release_date"] = date.today().isoformat()

    def transition_to(self, key: str, target: str) -> str:
        self.transitions.append((key, target))
        self.status_of[key] = target
        return target

    def status(self, key: str) -> str:
        return self.status_of.get(key, "To Do")

    def comment(self, key: str, text: str) -> str:
        self.comments.append((key, text))
        return str(len(self.comments))

    def recent_comments(self, key: str, limit: int = 10) -> list[dict]:
        return self.recent_comment_data.get(key, [])[:limit]

    def current_account_id(self) -> str:
        return self.jira_account_id

    def set_fields(self, key: str, values: dict) -> None:
        self.fields.append((key, values))

    def set_description(self, key: str, text: str) -> None:
        self.descriptions = getattr(self, "descriptions", []) + [(key, text)]

    def add_label(self, key: str, label: str) -> None:
        self.labels.append(("add", key, label))

    def remove_label(self, key: str, label: str) -> None:
        self.labels.append(("remove", key, label))

    def add_worklog(self, key, seconds, started, comment):
        self.worklogs = getattr(self, "worklogs", []) + [(key, seconds, comment)]

    initial_status = "Backlog"  # the status a new issue starts in

    def create_issue(self, project, summary, description, issuetype="Story", labels=None, parent="", fix_version="",
                     version_field=""):
        self.created = getattr(self, "created", [])
        key = f"{project}-{900 + len(self.created)}"
        created = {"key": key, "project": project, "summary": summary, "description": description,
                   "issuetype": issuetype, "labels": list(labels or []), "parent": parent}
        if fix_version:
            created["fix_version"] = fix_version
        if version_field:
            created["version_field"] = version_field
        self.created.append(created)
        self.status_of[key] = self.initial_status
        return key

    def link_issues(self, blocker, blocked, link_type="Blocks"):
        self.issue_links.append((blocker, blocked, link_type))

    def add_attachment(self, key, filename, content, content_type):
        self.attachments.append((key, filename, content, content_type))

class FakeGitHub:
    def __init__(self) -> None:
        self.meta = {"installation_id": 77}
        self.app_permissions = {
            "contents": "write", "pull_requests": "write", "workflows": "write",
            "actions": "read", "checks": "read", "statuses": "read", "administration": "write",
        }
        self.prs: list[dict] = []
        self.commits: list[dict] = []
        self.branches: list[tuple[str, str, str]] = []
        self.repositories = [{"full_name": "jooservices/test-aiflow-wp", "name": "test-aiflow-wp",
                              "default_branch": "develop"},
                             {"full_name": "jooservices/new-app", "name": "new-app",
                              "default_branch": "develop"},
                             {"full_name": "jooservices/aiflow-control", "name": "aiflow-control",
                              "default_branch": "develop"}]
        self.available_branches: dict[tuple[str, str], bool] = {}
        self.deleted_files: list[tuple[str, str]] = []
        self.repository_deletions: list[str] = []
        self.branch_heads: dict[tuple[str, str], str] = {}
        self.tags: dict[tuple[str, str], str] = {}
        self.releases: dict[tuple[str, str], dict] = {}
        self.merges: list[tuple[str, str, str, str]] = []
        self.files: dict[tuple[str, str, str], str] = {
            ("test-aiflow-wp", "develop", "README.md"): "# Test WordPress\n\nA test repo.\n",
            ("test-aiflow-wp", "develop", "package.json"): '{\n  "name": "test",\n  "version": "0.4.0"\n}\n',
            ("test-aiflow-wp", "develop", "CHANGELOG.md"): "# Changelog\n\n## 0.4.0\n- Previous release\n",
        }
        self.compare_results: dict[tuple[str, str, str], dict] = {}
        self.pr_by_number: dict[int, dict] = {}
        self.merge_commit_enabled = True
        self.merge_commit_enable_calls: list[str] = []

    def installation_repositories(self) -> list[dict[str, str]]:
        return list(self.repositories)

    def installation_details(self, installation_id: int) -> dict:
        assert installation_id == self.meta["installation_id"]
        return {"id": installation_id, "permissions": dict(self.app_permissions)}

    def branch_exists(self, repo: str, branch: str) -> bool:
        self.branches.append((repo, branch, "check"))
        return self.available_branches.get((repo, branch), branch == "develop" or (repo, branch) in self.branch_heads)

    def default_branch(self, repo):
        return "develop"

    def create_branch(self, repo, branch, base):
        self.branches.append((repo, branch, base))
        self.branch_heads[(repo, branch)] = self.branch_heads.get((repo, base), f"sha-{base}")
        for (file_repo, file_branch, path), content in list(self.files.items()):
            if file_repo == repo and file_branch == base:
                self.files[(repo, branch, path)] = content

    def delete_branch(self, repo, branch):
        self.deleted_branches = getattr(self, "deleted_branches", []) + [(repo, branch)]
        return self.branch_heads.pop((repo, branch), None) is not None

    def create_branch_at_sha(self, repo, branch, sha):
        self.branches.append((repo, branch, sha))
        self.branch_heads[(repo, branch)] = sha

    def repository_files(self, repo, ref):
        return sorted(path for (file_repo, branch, path) in self.files
                      if file_repo == repo and branch == ref)

    def file_content(self, repo, branch, path):
        return self.files.get((repo, branch, path))

    def commit_file(self, repo, branch, path, content, message):
        self.commits.append({"repo": repo, "branch": branch, "path": path, "content": content, "message": message})
        self.files[(repo, branch, path)] = content
        self.branch_heads[(repo, branch)] = f"{len(self.commits):040x}"
        return f"commit{len(self.commits):04d}"

    def delete_file(self, repo, branch, path, message):
        self.deleted_files.append((repo, path))
        self.commits.append({"repo": repo, "branch": branch, "path": path, "content": "", "message": message,
                             "deleted": True})
        return f"commit{len(self.commits):04d}"

    def token(self, repo=None, permissions=None):
        from datetime import datetime, timedelta, timezone
        self.token_requests = getattr(self, "token_requests", []) + [(repo, permissions)]
        return "ghs_fake", datetime.now(timezone.utc) + timedelta(hours=1)

    def create_pr(self, repo, head, base, title, body, draft=True):
        number = len(self.prs) + 1
        pr = {"repo": repo, "head": head, "base": base, "title": title, "body": body,
              "draft": draft, "number": number, "state": "open", "merged": False,
              "html_url": f"https://github.com/jooservices/{repo}/pull/{number}"}
        self.prs.append(pr)
        self.pr_by_number[number] = pr
        return pr

    def find_pr(self, repo, head):
        return next((pr for pr in self.prs if pr.get("repo") == repo
                     and (pr.get("head", {}).get("ref") if isinstance(pr.get("head"), dict) else pr.get("head")) == head
                     and pr.get("state") == "open"), None)

    def create_control_pr(self, repo, head, base, title, body):
        return self.create_pr(repo, head, base, title, body, draft=True)

    def bot_identity(self):
        return ("jooservices-aiflow-bot[bot]", "1+jooservices-aiflow-bot[bot]@users.noreply.github.com")

    def merge_pr(self, repo, number, title="", method="squash", sha=""):
        self.merge_pins = getattr(self, "merge_pins", []) + [sha]
        current = getattr(self, "pr_data", {}).get(number, {}).get("head", {}).get("sha", "")
        if sha and current and sha != current:  # GitHub refuses a merge whose head is not the pinned commit
            return {"merged": False, "sha": "", "message": "409: Head branch was modified. Review and try the merge again."}
        self.merged = getattr(self, "merged", []) + [(repo, number, title, method)]
        sha = f"merged-pr-{number:04d}"
        pr = self.pr_by_number.get(number)
        if pr:
            pr.update({"merged": True, "state": "closed", "merge_commit_sha": sha})
            self.branch_heads[(repo, pr["base"])] = sha
        return {"merged": True, "sha": sha if pr else "abc123", "message": "Pull Request successfully merged"}

    def update_branch(self, repo, number):
        self.branch_updates = getattr(self, "branch_updates", []) + [(repo, number)]
        return getattr(self, "update_branch_result", {"ok": True, "message": "202: Updating pull request branch."})

    def checks(self, repo, sha):
        return list(getattr(self, "check_data", {}).get(sha, []))

    def release_pr(self, repo, head, base):
        return next((pr for pr in self.prs if pr["repo"] == repo and pr["head"] == head
                     and pr["base"] == base), None)

    def update_pr_body(self, repo, number, body):
        pr = self.pr_by_number[number]
        pr["body"] = body
        return pr

    def reopen_pr(self, repo, number):
        pr = self.pr_by_number[number]
        pr.update({"state": "open", "merged": False})
        return pr

    def close_pr(self, repo, number):
        pr = self.pr_by_number.setdefault(number, {"number": number})
        pr.update({"state": "closed", "merged": False})
        return pr

    def enable_merge_commit(self, repo):
        if not self.merge_commit_enabled:
            self.merge_commit_enable_calls.append(repo)
            self.merge_commit_enabled = True

    def compare_branches(self, repo, base, head):
        return self.compare_results.get((repo, base, head), {"ahead_by": 1, "status": "ahead"})

    def pr(self, repo, number):
        return getattr(self, "pr_data", {}).get(number, {
            "number": number, "html_url": f"https://github.com/jooservices/{repo}/pull/{number}",
            "title": f"PR #{number}", "additions": 0, "deletions": 0, "changed_files": 0,
        })

    def comment(self, repo, number, body):
        self.comments = getattr(self, "comments", []) + [(number, body)]

    def branch_sha(self, repo, branch):
        return self.branch_heads.get((repo, branch), f"sha-{branch}")

    def merge_branch(self, repo, base, head, message):
        self.merges.append((repo, base, head, message))
        sha = f"merged-{len(self.merges):04d}"
        self.branch_heads[(repo, base)] = sha
        return {"sha": sha, "merged": True}

    def tag_sha(self, repo, tag):
        return self.tags.get((repo, tag))

    def create_tag(self, repo, tag, sha):
        self.tags[(repo, tag)] = sha

    def release_by_tag(self, repo, tag):
        return self.releases.get((repo, tag))

    def create_release(self, repo, tag, name, notes):
        release = {"html_url": f"https://github.com/jooservices/{repo}/releases/tag/{tag}",
                   "tag_name": tag, "name": name, "body": notes}
        self.releases[(repo, tag)] = release
        return release


@pytest.fixture
def ctx(db, leases, control_repo):
    s = Settings(join_token="join-secret", join_control_url="https://control.example.test",
                 reviewer_token="review-secret", owner_token="owner-secret", lease_ms=90_000,
                 jira_projects=["TESTWP"], canary_bug_project="TESTFLOW")
    snap = ControlRepo.load(control_repo)
    c = Ctx(settings=s, db=db, leases=leases, events=Events(db), repo=lambda: snap, jira=FakeJira(), github=FakeGitHub())
    # General integration fixtures exercise multi-node, multi-account flows; the licensing tests restore the
    # product's actual Free, Team and Business defaults before checking entitlement behavior.
    licensing.initialize(db)
    roomy_limits = {plan: {key: 1000 for key in limits} for plan, limits in licensing.PLAN_DEFAULTS.items()}
    db.run("UPDATE licensing_state SET plan_limits=%s WHERE id=1", (J(roomy_limits),))
    return c


@pytest.fixture
def client(ctx):
    from fastapi.testclient import TestClient
    app = create_app(ctx)
    app.state.sync_accounts()
    yield TestClient(app)
    # Readiness checks run on background threads; let them finish before the next test reuses the database.
    for thread in list(threading.enumerate()):
        if thread.name.startswith(("aiflow4-join-readiness-", "aiflow4-heartbeat-readiness-")):
            thread.join(timeout=15)
    system_check_api._PENDING_REFRESHES.clear()


def add_ticket(db, key: str, status: str = "To Do", labels=("aiflow-ready",), priority: str = "Medium", blocked_by=()):
    db.run("INSERT INTO tickets (key, project, status, labels, summary, description, priority, blocked_by)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, labels=EXCLUDED.labels",
           (key, key.split("-")[0], status, list(labels), f"Summary of {key}", "Acceptance criteria:\n- AC1: works",
            priority, list(blocked_by)))


def add_open_escalation(db, key: str, question: str = "Which option should be used?") -> None:
    """A ticket in Needs owner is offered to triage only while an escalation is open for it."""
    from aiflow.v4.control import escalations
    escalations.record(db, key, key.split("-")[0], "dev", "", escalations.AMBIGUITY, question)


def join_node(client, name: str, ip: str = "", role: str = "worker", project: str = "TESTWP"):
    issued = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": name,
                                                         "role": role, "project": project})
    assert issued.status_code == 200, issued.text
    joined = client.post("/v1/nodes/join", json={"join_token": issued.json()["token"], "ip": ip})
    if joined.status_code == 200:
        # A machine join starts a provider-backed readiness thread. Finish it before this helper seeds the
        # connected-agent baseline so its older join snapshot cannot overwrite that baseline afterwards.
        for thread in list(threading.enumerate()):
            if thread.name == f"aiflow4-join-readiness-{name}":
                thread.join(timeout=15)
        # The API tests model an installed agent: after join it reports a healthy baseline before tests change a
        # particular condition. Tests of the join-only result post directly to /v1/nodes/join instead.
        from aiflow.v4.core.system_check import condition
        from aiflow.v4.core.toolchains import pinned_version

        ctx = client.app.state.ctx
        repo = ctx.repo()
        project_key = joined.json()["project"]
        required = repo.tools_for_node(name, [project_key])
        tools = {}
        for tool in required:
            pin = pinned_version(repo.versions, tool, repo.projects[project_key].stacks)
            version = pin or "1.0"
            tools[tool] = f"{tool} {version}"
        tools.update({"git": "git version 2.43.0", "uv": "uv 0.8.1", "claude": "Claude Code 2.1.305",
                      "codex": "codex 1.0.0", "codex-code-mode-host": "codex-code-mode-host 1.0.0",
                      "opencode": "opencode 1.0.0", "grok": "grok 1.0.0", "php": "PHP 8.3.10",
                      "composer": "Composer 2.8.4", "wp": "WP-CLI 2.12.0", "node": "node v24.0.0",
                      "python3": "Python 3.12.2", "playwright": "Version 1.56.0",
                      "playwright-browser": "Playwright Chromium 1.56.0"})
        services = {name: {"state": "running", "healthy": True, "detail": "healthy"}
                    for name in repo.projects[project_key].required_services}
        credentials = {account.id: {"present": True, "valid": True} for account in repo.accounts_on(name)}
        from aiflow.v4.control.db import J
        from aiflow.v4.control.system_check import refresh_node

        metrics = {"disk_free": 20 * 1024 ** 3, "services": services}
        ctx.db.run("UPDATE nodes SET tools=%s,metrics=%s WHERE name=%s", (J(tools), J(metrics), name))
        readiness = refresh_node(
            ctx, name, "heartbeat",
            [condition("disk-space", "Free disk space", "ready", "20 GiB is available."),
             condition("workspace-permissions", "Work directory permissions", "ready",
                       "The node can write its work and state directories."),
             condition("control-network", "Control API network", "ready",
                       "The machine reached the control API and sent this heartbeat."),
             condition("repository-network", "Repository network", "ready",
                       "The machine can reach GitHub over HTTPS.")],
            credential_checks=credentials)
        # No HTTP heartbeat is sent by this helper, so retain the real joined status while modeling a ready agent
        # report for tests that exercise work scheduling. Trigger tests use direct join requests instead.
        for item in readiness["machine"]:
            if item["id"] == "node-heartbeat":
                item.update(result="ready", detail="The test agent is connected to the control API.", action="")
        ctx.db.run("UPDATE nodes SET readiness=%s WHERE name=%s", (J(readiness), name))
    return joined
