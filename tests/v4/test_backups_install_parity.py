"""JAIF-554: the backups routes install keeps its behaviour after being split.

The registrations (method, path, handler, signature) and the outcome of the configure and key routes for a table of
inputs were recorded from the unchanged code (tests/v4/fixtures/backups-install-parity.jsonl)."""
import contextlib
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.backups import routes
from aiflow.v4.control.features.backups.bodies import BackupConfigBody

GOLDEN = Path(__file__).with_name("fixtures") / "backups-install-parity.jsonl"


class RecordingApp:
    def __init__(self):
        self.registered = []

    def __getattr__(self, method):
        def factory(path, **options):
            def decorator(handler):
                self.registered.append({"kind": "route", "method": method.upper(), "path": path,
                                        "handler": handler.__name__,
                                        "signature": re.sub(r"0x[0-9a-f]+", "0x", str(inspect.signature(handler))),
                                        "options": options})
                return handler
            return decorator
        return factory


class FakeDb:
    def __init__(self, row):
        self.row, self.statements = row, []
        self.pool = self

    def one(self, sql, params=()):
        return self.row

    def run(self, sql, params=()):
        self.statements.append(("run", re.sub(r"\s+", " ", sql)[:60], [str(p) for p in params]))

    @contextlib.contextmanager
    def transaction(self):
        yield self


def console_user(request, permission=None, **kwargs):
    return SimpleNamespace(subject="owner@example.test")


def deps():
    return SimpleNamespace(console_user=console_user)


def make_ctx(tmp_path, row=None):
    settings = SimpleNamespace(
        backup_path=str(tmp_path / "durable"), control_repo=str(tmp_path / "repo"),
        control_secrets_dir=str(tmp_path / "secrets"), console_secrets_dir=str(tmp_path / "console"),
        github_app_dir=str(tmp_path / "github"), jira_secret_dir=str(tmp_path / "jira"),
        backup_key_file=str(tmp_path / "key" / "backup.key"))
    row = row if row is not None else {"id": True, "updated_at": None, "schedule": "daily", "retention_days": 30,
                                       "key_downloaded_at": None}
    return SimpleNamespace(settings=settings, db=FakeDb(row))


def test_registrations_match_the_recorded_golden(tmp_path):
    app = RecordingApp()
    routes.install(app, make_ctx(tmp_path), deps())
    expected = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    registered = [json.loads(json.dumps(item, default=str)) for item in app.registered]
    assert registered == [item for item in expected if item["kind"] == "route"]


CONFIGURE_CASES = {
    "local default": dict(),
    "local under durable folder": dict(destination_path="DURABLE/sub"),
    "local outside durable folder": dict(destination_path="/elsewhere/backups"),
    "relative folder": dict(destination_path="backups/here"),
    "root folder": dict(destination_path="/"),
    "dotdot in folder": dict(destination_path="/srv/aiflow/../etc"),
    "mounted anywhere": dict(destination_kind="mounted", destination_path="/mnt/nas/backups"),
    "mounted inside secrets": dict(destination_kind="mounted", destination_path="SECRETS/x"),
    "mounted containing the repo": dict(destination_kind="mounted", destination_path="/"),
    "mounted equal to repo": dict(destination_kind="mounted", destination_path="REPO"),
    "s3 ok": dict(destination_kind="s3", s3_endpoint="https://s3.example.test/", s3_bucket="my-bucket",
                  s3_prefix="a//b/../c/./d", s3_region=" eu-west-1 "),
    "s3 default region": dict(destination_kind="s3", s3_endpoint="https://s3.example.test", s3_bucket="bucket-1",
                              s3_region="  "),
    "s3 http endpoint": dict(destination_kind="s3", s3_endpoint="http://s3.example.test", s3_bucket="bucket-1"),
    "s3 endpoint with credentials": dict(destination_kind="s3", s3_endpoint="https://u:p@s3.example.test",
                                         s3_bucket="bucket-1"),
    "s3 endpoint with query": dict(destination_kind="s3", s3_endpoint="https://s3.example.test/?x=1",
                                   s3_bucket="bucket-1"),
    "s3 no bucket": dict(destination_kind="s3", s3_endpoint="https://s3.example.test", s3_bucket="  "),
    "s3 bad bucket": dict(destination_kind="s3", s3_endpoint="https://s3.example.test", s3_bucket="-bad"),
    "weekly retention": dict(schedule="weekly", retention_days=7, destination_path="DURABLE"),
    "durable folder itself": dict(destination_path="DURABLE"),
}


def _expand(values, tmp_path):
    return {key: (value.replace("DURABLE", str(tmp_path / "durable")).replace("SECRETS", str(tmp_path / "secrets"))
                  .replace("REPO", str(tmp_path / "repo")) if isinstance(value, str) else value)
            for key, value in values.items()}


def _configure(name, tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    published = []
    monkeypatch.setattr(routes, "publish_for", lambda _ctx, event: published.append(type(event).__name__))
    namespace = routes.install(RecordingApp(), ctx, deps())
    body = BackupConfigBody(**_expand(CONFIGURE_CASES[name], tmp_path))
    try:
        result = namespace.backup_configure(body, SimpleNamespace())
        outcome = {"ok": True, "result_keys": sorted(result)}
    except HTTPException as exc:
        outcome = {"ok": False, "status": exc.status_code, "detail": exc.detail}
    statements = [[s[0], s[1], [p.replace(str(tmp_path), "TMP") for p in s[2]]] for s in ctx.db.statements]
    return {"name": name, "outcome": outcome, "statements": statements, "published": published}


@pytest.mark.parametrize("name", list(CONFIGURE_CASES))
def test_configure_matches_the_recorded_outcome(name, tmp_path, monkeypatch):
    expected = {item["name"]: item for item in
                (json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines())
                if item["kind"] == "configure"}
    assert _configure(name, tmp_path, monkeypatch) == {k: v for k, v in expected[name].items() if k != "kind"}


def test_status_and_run_routes(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    published = []
    monkeypatch.setattr(routes, "publish_for", lambda _ctx, event: published.append(type(event).__name__))
    namespace = routes.install(RecordingApp(), ctx, deps())
    monkeypatch.setattr(routes.control_backups, "list_backups", lambda config: [{"name": "b1"}])
    status = namespace.backup_status(SimpleNamespace())
    assert status["backups"] == [{"name": "b1"}] and status["key_available"] is False and status["key_downloaded"] is False
    assert status["storage_error"] == "" and "id" not in status and "updated_at" not in status
    assert namespace.backup_request(SimpleNamespace()) == {"queued": True}
    assert published == ["BackupRunRequested"] and ctx.db.statements[-1][1].startswith("UPDATE control_backup_config SET")
    assert namespace.backup_config(False).get("backups") is None


def test_missing_settings_row_is_a_503(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.db.row = None
    namespace = routes.install(RecordingApp(), ctx, deps())
    with pytest.raises(HTTPException) as failed:
        namespace.backup_config()
    assert (failed.value.status_code, failed.value.detail) == (503, "backup settings are not initialized")
