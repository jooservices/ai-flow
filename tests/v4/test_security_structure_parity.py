"""JAIF-533: the security installation keeps its middleware and route order (observable behaviour) after the split."""
from aiflow.v4.control.api import create_app

SECRET = "s" * 40


def _app(ctx):
    ctx.settings.console_session_secret = SECRET
    ctx.settings.console_login = False
    return create_app(ctx)


def _security_routes(app):
    return [(route.path, sorted(route.methods or ())) for route in app.routes
            if getattr(route, "path", "").startswith("/v1/security/")]


def _middleware_names(app):
    return [getattr(item.kwargs.get("dispatch"), "__name__", str(item.cls)) for item in app.user_middleware]


EXPECTED_ROUTES = [
    ("/v1/security/settings", ["GET"]), ("/v1/security/settings", ["PUT"]), ("/v1/security/sessions", ["GET"]),
    ("/v1/security/sessions/{sid}/revoke", ["POST"]), ("/v1/security/users/{email}/sign-out", ["POST"]),
    ("/v1/security/users", ["GET"]), ("/v1/security/users/{email}/mfa/reset", ["POST"]),
    ("/v1/security/users/{email}/unlock", ["POST"]), ("/v1/security/mfa", ["GET"]), ("/v1/security/mfa/setup", ["POST"]),
    ("/v1/security/mfa/enable", ["POST"]), ("/v1/security/mfa/recovery-codes", ["POST"]),
    ("/v1/security/mfa/disable", ["POST"]), ("/v1/security/audit", ["GET"]),
]
EXPECTED_MIDDLEWARE = ["limits_and_headers", "csrf", "audit_changes", "load_public_address", "isolate_runtime_repo_snapshot"]


def test_security_routes_keep_their_registration_order(ctx):
    assert _security_routes(_app(ctx)) == EXPECTED_ROUTES


def test_middleware_order_keeps_gate_audit_csrf_and_limits(ctx):
    assert _middleware_names(_app(ctx)) == EXPECTED_MIDDLEWARE



# --- pure helpers of the split parts ---------------------------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from aiflow.v4.control import security_middleware as middleware  # noqa: E402
from aiflow.v4.control.security_settings import SettingsBody, requested_changes  # noqa: E402


def _request(path="/v1/x", method="GET", cookies=None, headers=None):
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method, cookies=cookies or {}, headers=headers or {})


def _kit(**settings):
    values = {"rate_limit_per_min": 0, "rate_limit_public_per_min": 0, "owner_token": ""}
    values.update(settings)
    return SimpleNamespace(settings=SimpleNamespace(**values))


def test_explicit_audit_paths_are_the_people_and_security_routes():
    for path in ("/v1/security/mfa/setup", "/v1/console/users/a@b.test", "/v1/auth/logout", "/logout"):
        assert middleware.is_explicit_audit(path)
    for path in ("/v1/tickets", "/v1/integrations/jira/x", "/login"):
        assert not middleware.is_explicit_audit(path)


def test_request_budgets_add_the_all_budget_and_the_public_or_anonymous_path_budget():
    kit = _kit(rate_limit_per_min=100, rate_limit_public_per_min=10)
    assert middleware.request_budgets(kit, _request("/v1/x", "GET", cookies={"c": "1"})) == [("all", 100)]
    assert middleware.request_budgets(kit, _request("/v1/x", "POST")) == [("all", 100), ("path:/v1/x", 10)]
    assert middleware.request_budgets(kit, _request("/v1/x", "POST", headers={"authorization": "Bearer x"})) == [("all", 100)]
    assert middleware.request_budgets(_kit(), _request("/v1/x", "POST")) == []


def test_login_has_no_generic_budget():
    kit = _kit(rate_limit_per_min=1, trusted_proxies=[])
    assert middleware.over_budget(kit, _request("/v1/auth/login", "POST")) == 0


def test_owner_token_actor_needs_the_exact_bearer_token():
    kit = _kit(owner_token="owner-secret-value")
    assert middleware.owner_token_actor(kit, _request(headers={"authorization": "Bearer owner-secret-value"})) == "owner-token"
    assert middleware.owner_token_actor(kit, _request(headers={"authorization": "Bearer other"})) == ""
    assert middleware.owner_token_actor(kit, _request(headers={"authorization": "owner-secret-value"})) == ""
    assert middleware.owner_token_actor(_kit(), _request(headers={"authorization": "Bearer "})) == ""


def test_requested_changes_keeps_only_the_fields_that_were_sent():
    assert requested_changes(SettingsBody()) == {}
    assert requested_changes(SettingsBody(session_ttl_s=3600)) == {"session_ttl_s": 3600}
    assert requested_changes(SettingsBody(session_ttl_s=3600, require_mfa=True)) == {"session_ttl_s": 3600,
                                                                                    "require_mfa": True}
