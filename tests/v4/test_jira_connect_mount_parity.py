"""Characterize Jira route registration and HTTP behavior before splitting mount()."""
from __future__ import annotations

import json
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException, Request, Response

from aiflow.v4.control import jira_connect

FIXTURE = Path(__file__).parent / "fixtures" / "jira_connect_mount_parity.jsonl"
OWNER = {"Authorization": "Bearer owner-key"}
JIRA = "/v1/integrations/jira"
START, CALLBACK, TOKEN, DISCONNECT = (JIRA + suffix for suffix in
                                      ("/oauth/start", "/oauth/callback", "/api-token", "/disconnect"))


def _case(name, method=None, path=None, **values):
    return {"name": name, "method": method, "path": path, **values}


TABLE = tuple(json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines())
_LIMIT_VALUES = {"<CLIENT_ID_200>": "C" * 200, "<CLIENT_SECRET_500>": "TEXT-" * 100,
                 "<EMAIL_320>": "a" * 313 + "@x.test", "<API_TOKEN_2000>": "TEXT-" * 400,
                 "<SITE_300>": "https://" + "a" * 292}


def _expand_case(case):
    expanded = dict(case)
    if isinstance(expanded.get("body"), dict):
        expanded["body"] = {key: _LIMIT_VALUES.get(value, value) for key, value in expanded["body"].items()}
    return expanded


class _Store:
    def __init__(self, calls, initial):
        self.calls, self.value = calls, dict(initial)

    def app(self):
        self.calls.append(["store.app"])
        return dict(self.value)

    def save_app(self, client_id, client_secret):
        self.calls.append(["store.save_app", _summary(client_id), _summary(client_secret)])
        self.value = {"client_id": client_id, "client_secret": client_secret}


class _Auth:
    def __init__(self, spec, calls):
        self.spec, self.calls = spec, calls
        self.store = _Store(calls, spec.get("initial_app", {}))

    def status(self):
        self.calls.append(["auth.status"])
        return {"connected": False, "source": "none", "state": "disconnected"}

    def authorize_url(self, state):
        self.calls.append(["auth.authorize_url", len(state)])
        return "https://auth.example.test/authorize?state=" + state

    def connect_oauth(self, code, actor):
        self.calls.append(["auth.connect_oauth", code, actor])
        if self.spec.get("oauth_error"):
            raise jira_connect.OAuthFailed(self.spec["oauth_error"])
        return {"kind": "oauth", "site_name": "Example Jira", "site_url": "https://jira.example.test",
                "cloud_id": "cloud-test", "scopes": ["read:jira-work"]}

    def connect_api_token(self, email, token, site, actor):
        self.calls.append(["auth.connect_api_token", _summary(email), _summary(token), site, actor])
        if self.spec.get("api_error"):
            raise jira_connect.OAuthFailed(self.spec["api_error"])
        return {"kind": "basic", "site_name": "Example Jira", "site_url": site, "cloud_id": "cloud-test"}

    def disconnect(self, actor):
        revoked = self.spec.get("revoked", True)
        self.calls.append(["auth.disconnect", actor])
        return {"revoked": revoked, "data": {"kind": "oauth", "site_name": "Example Jira"}}


class _Pipeline:
    def __init__(self, redis, calls):
        self.redis, self.calls, self.key = redis, calls, None

    def get(self, key):
        self.key = key
        self.calls.append(["redis.get", _key(key)])
        return self

    def delete(self, key):
        self.calls.append(["redis.delete", _key(key)])
        return self

    def execute(self):
        raw = self.redis.values.pop(self.key, None)
        self.calls.append(["redis.execute", bool(raw)])
        return raw, int(raw is not None)


class _Redis:
    def __init__(self, calls):
        self.calls, self.values = calls, {}

    def set(self, key, value, ex):
        data = json.loads(value)
        self.calls.append(["redis.set", _key(key), {"actor": data["actor"], "at": "<time>"}, ex])
        self.values[key] = value

    def pipeline(self):
        self.calls.append(["redis.pipeline"])
        return _Pipeline(self, self.calls)


def _summary(value):
    return {"length": len(value), "first": value[:1], "last": value[-1:]} if isinstance(value, str) else ""


def _key(value):
    return "<state-key>" if "jira_oauth_state:" in value else value


def _route_inventory():
    app = FastAPI()
    jira_connect.mount(app, object())
    routes = [[route.path, sorted(route.methods), route.name] for route in app.routes
              if route.path.startswith("/v1/integrations/jira")]
    return {"name": "route_inventory", "routes": routes}


def _context_for_case(spec, calls, monkeypatch):
    redis = _Redis(calls)
    settings = SimpleNamespace(owner_token="owner-key", console_session_secret="x" * 40,
                               license_public_key="", public_url=spec.get("public_url", "https://control.example.test"),
                               jira_site="https://jira.example.test")
    ctx = SimpleNamespace(settings=settings, db=object(), leases=SimpleNamespace(p="test:", r=redis),
                          events=SimpleNamespace(emit=lambda *args, **kwargs: calls.append(["event", *args, kwargs])))
    auth = None if spec.get("auth_missing") else _Auth(spec, calls)
    ctx.jira_auth = auth
    monkeypatch.setattr(jira_connect, "write_audit",
                        lambda _ctx, action, actor, accepted, result, payload=None:
                        calls.append(["audit", action, actor, accepted, result, payload or {}]))
    monkeypatch.setattr(jira_connect, "close_incident",
                        lambda *_args: calls.append(["close_incident", *_args[1:]]))
    monkeypatch.setattr(jira_connect.licensing, "require_writable",
                        lambda *_args: _license(calls, spec.get("license_error")))
    user_type = spec.get("user")
    claims = object() if user_type else None
    user = SimpleNamespace(subject="person@example.test", effective_roles={"integrations.manage"})
    monkeypatch.setattr(jira_connect, "verify_session", lambda *_args: claims)
    monkeypatch.setattr(jira_connect, "resolve_session_user", lambda *_args: user if claims else None)
    monkeypatch.setattr(jira_connect, "role_satisfies", lambda *_args: user_type == "allowed")
    return ctx, redis


def _request_for_case(spec):
    headers = dict(spec.get("headers", {}))
    if spec.get("cookie"):
        headers["Cookie"] = f"{jira_connect.STATE_COOKIE}={spec['cookie']}"
    method, parsed = spec["method"], urlsplit(spec["path"])
    body = json.dumps(spec["body"]).encode() if "body" in spec else spec.get("raw", "").encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "https", "path": parsed.path, "raw_path": parsed.path.encode(),
             "query_string": parsed.query.encode(), "root_path": "", "headers": [(k.lower().encode(), v.encode())
                                                                                  for k, v in headers.items()],
             "client": ("testclient", 50000), "server": ("control.example.test", 443)}

    async def receive(): return {"type": "http.request", "body": body, "more_body": False}

    return method, parsed, Request(scope, receive)


def _route_arguments(app, method, parsed, request):
    route = next(route for route in app.routes if route.path == parsed.path and method in route.methods)
    query = {key: values[-1] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
    arguments = {name: request if name == "request" else query.get(name, "")
                 for name in inspect.signature(route.endpoint).parameters}
    return route, arguments


def _invoke_route(route, arguments):
    try:
        response = route.endpoint(**arguments)
        if inspect.isawaitable(response):
            response = asyncio.run(response)
        return _response_details(response)
    except HTTPException as error:
        return {"status": error.status_code, "body": {"detail": error.detail},
                "headers": {key.lower(): value for key, value in (error.headers or {}).items()}}


def _response_details(response):
    status = response.status_code if isinstance(response, Response) else 200
    headers = {}
    if isinstance(response, Response):
        for header in ("location", "set-cookie"):
            if header in response.headers:
                headers[header] = response.headers[header]
        body = json.loads(response.body) if response.body and response.media_type == "application/json" else response.body.decode()
    else:
        body = response
    if isinstance(body, dict) and "authorize_url" in body:
        body["authorize_url"] = "<authorize-url>"
    _normalize_cookie(headers)
    return {"status": status, "body": body, "headers": headers}


def _normalize_cookie(headers):
    if "set-cookie" in headers:
        cookie, separator, attributes = headers["set-cookie"].partition(";")
        name, _, _value = cookie.partition("=")
        attrs = ["expires=<time>" if item.strip().lower().startswith("expires=") else item.strip()
                 for item in attributes.split(";")]
        headers["set-cookie"] = f"{name}=<state>" + (separator + "; ".join(attrs) if separator else "")


def observe_case(spec, monkeypatch):
    if spec["name"] == "route_inventory":
        return _route_inventory()
    calls = []
    ctx, redis = _context_for_case(spec, calls, monkeypatch)

    app = FastAPI()
    jira_connect.mount(app, ctx)
    if spec.get("pending") is not None:
        redis.values[jira_connect._state_key(ctx, "state-a")] = json.dumps({"at": 1, **spec["pending"]})
    method, parsed, request = _request_for_case(spec)
    route, arguments = _route_arguments(app, method, parsed, request)
    return {"name": spec["name"], "response": _invoke_route(route, arguments), "calls": calls}


def _license(calls, error):
    calls.append(["license"])
    if error:
        raise jira_connect.licensing.LicenseError(error)


@pytest.mark.parametrize("row", TABLE, ids=lambda row: row["case"]["name"])
def test_mount_matches_recorded_behavior(row, monkeypatch):
    assert observe_case(_expand_case(row["case"]), monkeypatch) == row["expected"]
