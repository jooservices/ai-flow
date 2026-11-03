"""Pin the knowledge route registrations before and after installer extraction."""
import inspect
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.knowledge import routes_resources
from aiflow.v4.control.features.knowledge.routes import install


FIXTURE = Path(__file__).with_name("fixtures") / "knowledge-install-parity.jsonl"


class StableDependencyDefault:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Depends({self.name})"


def signature_text(handler):
    signature = inspect.signature(handler)
    parameters = [
        parameter.replace(default=StableDependencyDefault(
            getattr(parameter.default.dependency, "__name__", type(parameter.default.dependency).__name__)))
        if hasattr(parameter.default, "dependency") else parameter
        for parameter in signature.parameters.values()
    ]
    return str(signature.replace(parameters=parameters))


class RecordingApp:
    def __init__(self):
        self.registrations = []

    def __getattr__(self, method):
        def register(path, **kwargs):
            def decorate(handler):
                self.registrations.append({
                    "method": method.upper(),
                    "path": path,
                    "handler": handler.__name__,
                    "signature": signature_text(handler),
                    "kwargs": kwargs,
                })
                return handler
            return decorate
        return register


def test_knowledge_route_registration_matches_the_checked_in_golden():
    app = RecordingApp()
    def owner_auth():
        return "owner"

    installed = install(app, SimpleNamespace(), SimpleNamespace(console_user=lambda *_args, **_kwargs: None,
                                                                 owner_auth=owner_auth))
    live = [json.dumps(row, separators=(",", ":"), sort_keys=True) for row in app.registrations]

    assert live == FIXTURE.read_text(encoding="utf-8").splitlines()
    assert tuple(vars(installed)) == (
        "knowledge_items", "knowledge_item_save", "knowledge_item_action", "load_managed_resources",
        "submit_resource_change", "knowledge_resources", "knowledge_history", "knowledge_save",
        "knowledge_restore", "knowledge_approve",
    )
    assert str(inspect.signature(installed.load_managed_resources)) == "() -> None"
    assert str(inspect.signature(installed.submit_resource_change)) == (
        "(kind: str, resource_id: str, title: str, content: str, archived: bool, actor: str, action: str, "
        "expected_version: int | None = None) -> dict"
    )


def test_load_managed_resources_imports_into_the_context_database(monkeypatch):
    calls = []
    monkeypatch.setattr(routes_resources, "knowledge",
                        SimpleNamespace(import_local=lambda db, root: calls.append((db, root))))
    db = object()
    ctx = SimpleNamespace(db=db, repo=lambda: SimpleNamespace(root="control-repo"))

    routes_resources._load_managed_resources(ctx)

    assert calls == [(db, "control-repo")]


def test_submit_resource_change_maps_invalid_input_before_external_calls(monkeypatch):
    class Invalid(Exception):
        pass

    def reject(*_args):
        raise Invalid("invalid resource")

    monkeypatch.setattr(routes_resources, "knowledge", SimpleNamespace(Invalid=Invalid, validate=reject))
    monkeypatch.setattr(routes_resources, "github_control_gateway",
                        lambda _ctx: pytest.fail("invalid input must fail before GitHub access"))

    with pytest.raises(HTTPException) as error:
        routes_resources._submit_resource_change(SimpleNamespace(), "bad", "id", "title", "content", False,
                                                  "actor", "save")

    assert error.value.status_code == 400 and error.value.detail == "invalid resource"


def test_policy_proposal_helper_keeps_database_and_event_order(monkeypatch):
    calls = []

    class Database:
        @contextmanager
        def transaction(self):
            calls.append("transaction-start")
            yield
            calls.append("transaction-end")

        def run(self, query, params):
            calls.append(("insert", query, params))

    class Github:
        def default_branch(self, _repo_name):
            pytest.fail("the base branch is supplied by the caller")

        def create_branch(self, _repo_name, branch, base):
            calls.append(("branch", branch, base))

        def commit_file(self, _repo_name, _branch, _path, _payload, _message):
            calls.append("commit")
            return "commit-sha"

        def create_control_pr(self, _repo_name, _branch, _base, _title, _body):
            calls.append("pull-request")
            return {"number": 7, "html_url": "https://github.test/pull/7"}

    monkeypatch.setattr(routes_resources.secrets, "token_hex", lambda _size: "abcd")
    monkeypatch.setattr(routes_resources, "publish_for", lambda _ctx, _event: calls.append("publish"))
    ctx = SimpleNamespace(db=Database(), events=SimpleNamespace(emit=lambda *_args, **_kwargs: calls.append("emit")))

    result = routes_resources._propose_policy_change(ctx, Github(), "control", "main", "default", "Title",
                                                      "content", False, "owner", "save", None, "policy.md", "file")

    assert result == {"pending": True, "type": "policy", "id": "default", "title": "Title",
                     "pr_number": 7, "pr_url": "https://github.test/pull/7", "version": 0}
    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "branch", "commit", "pull-request", "transaction-start", "insert", "publish", "transaction-end", "emit",
    ]
