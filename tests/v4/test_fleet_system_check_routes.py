"""The System check and support-bundle routes registered by the fleet feature keep their auth and responses."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from aiflow.v4.control.features.fleet import routes_system_check
from aiflow.v4.control.features.fleet.routes_system_check import install_system_check_routes


class RecordingApp:
    def __init__(self):
        self.handlers = {}

    def _route(self, method):
        def factory(path):
            def decorator(handler):
                self.handlers[(method, path)] = handler
                return handler
            return decorator
        return factory

    def __getattr__(self, method):
        return self._route(method.upper())


def _install(console_login: bool, monkeypatch):
    system_check = MagicMock()
    system_check.report_nodes.return_value = [{"name": "box"}]
    system_check.refresh_all.return_value = [{"name": "box", "trigger": "manual"}]
    system_check.support_bundle.return_value = {"system_check": {}}
    monkeypatch.setattr(routes_system_check, "system_check", system_check)
    user = SimpleNamespace(project_keys={"alpha"})
    deps = SimpleNamespace(console_user=MagicMock(return_value=user),
                           optional_console_user=MagicMock(return_value=None))
    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=console_login))
    app = RecordingApp()
    install_system_check_routes(app, ctx, deps)
    return app, deps, system_check


def test_routes_are_registered_in_order(monkeypatch):
    app, _, _ = _install(True, monkeypatch)
    assert list(app.handlers) == [("GET", "/v1/system-check"), ("POST", "/v1/system-check/run"),
                                  ("GET", "/v1/support-bundle")]


def test_report_requires_console_user_when_login_is_on(monkeypatch):
    app, deps, system_check = _install(True, monkeypatch)
    request = object()
    assert app.handlers[("GET", "/v1/system-check")](request) == {"nodes": [{"name": "box"}]}
    deps.console_user.assert_called_once_with(request, "node.operations.read")
    assert system_check.report_nodes.call_args.args[1] == {"alpha"}


def test_report_without_login_uses_optional_user(monkeypatch):
    app, deps, system_check = _install(False, monkeypatch)
    app.handlers[("GET", "/v1/system-check")](object())
    deps.console_user.assert_not_called()
    assert system_check.report_nodes.call_args.args[1] is None


def test_run_needs_nodes_manage_and_refreshes_everything(monkeypatch):
    app, deps, system_check = _install(True, monkeypatch)
    request = object()
    result = app.handlers[("POST", "/v1/system-check/run")](request)
    deps.console_user.assert_called_once_with(request, "nodes.manage", target="system-check", action="run")
    assert result == {"nodes": [{"name": "box", "trigger": "manual"}]}
    assert system_check.refresh_all.call_args.args[1] == "manual"


def test_support_bundle_is_a_no_store_attachment(monkeypatch):
    app, deps, system_check = _install(True, monkeypatch)
    request = object()
    response = app.handlers[("GET", "/v1/support-bundle")](request)
    deps.console_user.assert_called_once_with(request, "node.operations.read")
    assert system_check.support_bundle.call_args.args[1] == {"alpha"}
    assert response.headers["content-disposition"] == 'attachment; filename="aiflow4-support.json"'
    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {"system_check": {}}
