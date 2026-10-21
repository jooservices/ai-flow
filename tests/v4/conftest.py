"""Fixtures: a real Redis and PostgreSQL (AIFLOW4_TEST_REDIS_URL / AIFLOW4_TEST_DATABASE_URL), a control repo on
disk, and fakes for Jira and GitHub so API flows run without the network."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import redis

from aiflow.v4.control.api import Ctx, create_app
from aiflow.v4.control.db import DB
from aiflow.v4.control.events import Events
from aiflow.v4.control.jira import Jira
from aiflow.v4.core.controlrepo import ControlRepo
from aiflow.v4.core.fencing import Leases
from aiflow.v4.core.settings import Settings

REDIS_URL = os.environ.get("AIFLOW4_TEST_REDIS_URL", "redis://127.0.0.1:16399/5")
DATABASE_URL = os.environ.get("AIFLOW4_TEST_DATABASE_URL", "postgresql://aiflow:test@127.0.0.1:15499/aiflow4")


@pytest.fixture
def leases():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    prefix = f"t{uuid.uuid4().hex[:8]}:"
    yield Leases(r, prefix)
    for k in r.scan_iter(match=prefix + "*"):
        r.delete(k)


@pytest.fixture(scope="session")
def test_database_url():
    """A database of its own for this pytest session (created next to the configured one, dropped at the end), so
    parallel dev runs sharing one PostgreSQL server never drop each other's schema or wait on its migration lock.
    Falls back to the configured database when the role may not create databases."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    name = f"aiflow4_t_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(DATABASE_URL, autocommit=True)
        template = os.environ.get("AIFLOW4_TEST_DATABASE_TEMPLATE")  # a database that already carries extensions (pgvector)
        admin.execute(sql.SQL("CREATE DATABASE {}" + (" TEMPLATE {}" if template else ""))
                      .format(sql.Identifier(name), *([sql.Identifier(template)] if template else [])))
    except psycopg.Error:
        yield DATABASE_URL
        return
    own = make_conninfo(DATABASE_URL, dbname=name)
    try:
        yield own
    finally:
        try:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        except psycopg.Error:
            pass
        admin.close()


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
        self.fields: list[tuple[str, dict]] = []
        self.labels: list[tuple[str, str, str]] = []
        self.attachments: list[tuple[str, str, bytes, str]] = []

    def transition_to(self, key: str, target: str) -> str:
        self.status_of[key] = target
        return target

    def status(self, key: str) -> str:
        return self.status_of.get(key, "To Do")

    def comment(self, key: str, text: str) -> str:
        self.comments.append((key, text))
        return str(len(self.comments))

    def recent_comments(self, key: str, limit: int = 10) -> list[dict]:
        return self.recent_comment_data.get(key, [])[:limit]

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

    def create_issue(self, project, summary, description, issuetype="Story", labels=None, parent=""):
        self.created = getattr(self, "created", [])
        key = f"{project}-{900 + len(self.created)}"
        self.created.append({"key": key, "project": project, "summary": summary, "description": description,
                             "issuetype": issuetype, "labels": list(labels or []), "parent": parent})
        self.status_of[key] = self.initial_status
        return key

    def add_attachment(self, key, filename, content, content_type):
        self.attachments.append((key, filename, content, content_type))

class FakeGitHub:
    def __init__(self) -> None:
        self.prs: list[dict] = []

    def token(self, repo=None, permissions=None):
        from datetime import datetime, timedelta, timezone
        self.token_requests = getattr(self, "token_requests", []) + [(repo, permissions)]
        return "ghs_fake", datetime.now(timezone.utc) + timedelta(hours=1)

    def create_pr(self, repo, head, base, title, body, draft=True):
        pr = {"html_url": f"https://github.com/jooservices/{repo}/pull/{len(self.prs) + 1}", "number": len(self.prs) + 1}
        self.prs.append({"repo": repo, "head": head, "base": base, "title": title, "draft": draft})
        return pr

    def bot_identity(self):
        return ("jooservices-aiflow-bot[bot]", "1+jooservices-aiflow-bot[bot]@users.noreply.github.com")

    def merge_pr(self, repo, number, title="", method="squash"):
        self.merged = getattr(self, "merged", []) + [(repo, number, title, method)]
        return {"merged": True, "sha": "abc123", "message": "Pull Request successfully merged"}

    def pr(self, repo, number):
        return getattr(self, "pr_data", {}).get(number, {
            "number": number, "html_url": f"https://github.com/jooservices/{repo}/pull/{number}",
            "title": f"PR #{number}", "additions": 0, "deletions": 0, "changed_files": 0,
        })

    def comment(self, repo, number, body):
        self.comments = getattr(self, "comments", []) + [(number, body)]


@pytest.fixture
def ctx(db, leases, control_repo):
    s = Settings(join_token="join-secret", join_control_url="https://control.example.test",
                 reviewer_token="review-secret", lease_ms=90_000, jira_projects=["TESTWP"])
    snap = ControlRepo.load(control_repo)
    c = Ctx(settings=s, db=db, leases=leases, events=Events(db), repo=lambda: snap, jira=FakeJira(), github=FakeGitHub())
    return c


@pytest.fixture
def client(ctx):
    from fastapi.testclient import TestClient
    app = create_app(ctx)
    app.state.sync_accounts()
    return TestClient(app)


def add_ticket(db, key: str, status: str = "To Do", labels=("aiflow-ready",), priority: str = "Medium", blocked_by=()):
    db.run("INSERT INTO tickets (key, project, status, labels, summary, description, priority, blocked_by)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, labels=EXCLUDED.labels",
           (key, key.split("-")[0], status, list(labels), f"Summary of {key}", "Acceptance criteria:\n- AC1: works",
            priority, list(blocked_by)))
