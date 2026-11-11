"""The Master review-request endpoint is served by its discovered router."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control import route_deps
from aiflow.v4.control.api import create_app
from aiflow.v4.control.features.runs import master_cycles
from aiflow.v4.control.route_registry import concrete_routes
from aiflow.v4.control.routes import master_review_request


def _request(ctx):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))


def test_handler_uses_shared_context_and_console_write_helpers_in_order(monkeypatch):
    ctx = object()
    request = _request(ctx)
    calls = []
    monkeypatch.setattr(route_deps, "context", lambda received: calls.append(("context", received)) or ctx)
    monkeypatch.setattr(
        route_deps, "console_user_for",
        lambda received, permission, **options: calls.append(("console", received, permission, options)))
    monkeypatch.setattr(master_cycles, "request_review", lambda received: calls.append(("review", received)))

    response = master_review_request.master_review_request(request)

    assert response == {"requested": True}
    assert calls == [
        ("context", request),
        ("console", request, "console.write", {"target": "master", "action": "review-request"}),
        ("review", ctx),
    ]


def test_handler_refusal_does_not_request_a_review(monkeypatch):
    calls = []

    def refuse(*_args, **_kwargs):
        calls.append("refused")
        raise HTTPException(403, "not available for your role")

    monkeypatch.setattr(route_deps, "console_user_for", refuse)
    monkeypatch.setattr(master_cycles, "request_review", lambda _ctx: calls.append("requested"))

    with pytest.raises(HTTPException) as caught:
        master_review_request.master_review_request(_request(object()))

    assert caught.value.status_code == 403
    assert calls == ["refused"]


def test_handler_propagates_review_storage_errors(monkeypatch):
    failure = RuntimeError("review storage unavailable")
    monkeypatch.setattr(route_deps, "console_user_for", lambda *_args, **_kwargs: None)

    def fail(_ctx):
        raise failure

    monkeypatch.setattr(master_cycles, "request_review", fail)

    with pytest.raises(RuntimeError) as caught:
        master_review_request.master_review_request(_request(object()))

    assert caught.value is failure


def test_assembled_app_serves_the_review_request_once_from_discovery(ctx):
    matches = [route for route in concrete_routes(create_app(ctx).routes)
               if "POST" in (route.methods or ()) and route.path == "/v1/master/review-requests"]

    assert len(matches) == 1
    assert matches[0].endpoint.__module__ == "aiflow.v4.control.routes.master_review_request"
