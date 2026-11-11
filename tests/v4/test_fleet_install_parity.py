"""Pin the fleet route registrations while the installer is split into route groups."""
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from aiflow.v4.control.features.fleet import routes
from aiflow.v4.control.api import create_app
from aiflow.v4.control.route_registry import concrete_routes
from tests.v4.conftest import join_node


FIXTURE = Path(__file__).parent / "fixtures" / "fleet-install-routes.jsonl"
RETURNED_NAMES = (
    "issue_join_token", "inspect_join_token", "join", "node_heartbeat", "node_health", "operation_plan",
    "create_join_command", "JOB_ROLES", "account_jobs", "lock_machine_capacity", "machine_capacity",
    "save_managed_account", "add_managed_account", "update_managed_account", "restart_account_sign_in",
    "account_sign_in_details", "report_account_sign_in_status", "complete_account_sign_in",
    "remove_managed_account", "set_node_work_acceptance", "remove_managed_node", "create_node_operation",
    "node_operations", "probe_project", "check_probe_token", "save_check_probe", "next_node_operation",
    "checkpoint_node_operation", "finish_node_operation",
)


def node_auth():
    return "node"


def _annotation_text(annotation):
    if annotation is inspect.Signature.empty:
        return ""
    return annotation.__name__ if isinstance(annotation, type) else str(annotation)


def _signature_text(handler):
    signature = inspect.signature(handler)
    params = []
    for parameter in signature.parameters.values():
        item = parameter.name
        annotation = _annotation_text(parameter.annotation)
        if annotation:
            item += f": {annotation}"
        if parameter.default is not inspect.Signature.empty:
            default = parameter.default
            if type(default).__name__ == "Depends":
                dependency = default.dependency
                name = dependency.__name__ if dependency is not None else "None"
                default = f"Depends({name})"
            else:
                default = repr(default)
            item += f" = {default}"
        params.append(item)
    result = f"({', '.join(params)})"
    returned = _annotation_text(signature.return_annotation)
    return f"{result} -> {returned}" if returned else result


def _json_value(value):
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return type(value).__name__


class _RecordingApp:
    def __init__(self):
        self.registrations = []

    def _register(self, method, path, kwargs):
        def decorate(handler):
            self.registrations.append([
                method, path, handler.__name__, _signature_text(handler), _json_value(kwargs),
            ])
            return handler
        return decorate

    def get(self, path, **kwargs):
        return self._register("get", path, kwargs)

    def post(self, path, **kwargs):
        return self._register("post", path, kwargs)

    def put(self, path, **kwargs):
        return self._register("put", path, kwargs)

    def patch(self, path, **kwargs):
        return self._register("patch", path, kwargs)

    def delete(self, path, **kwargs):
        return self._register("delete", path, kwargs)

    def api_route(self, path, **kwargs):
        return self._register("api_route", path, kwargs)

    def middleware(self, path, **kwargs):
        return self._register("middleware", path, kwargs)

    def on_event(self, path, **kwargs):
        return self._register("on_event", path, kwargs)


def test_fleet_install_route_registrations_match_unchanged_golden():
    app = _RecordingApp()
    deps = SimpleNamespace(
        audit=None, audit_in=None, console_user=None, optional_console_user=None, create_join_grant=None,
        current_release=None,
        fleet_release=None, node_auth=node_auth, release_assets=None, require_capacity=None,
    )

    installed = routes.install(app, SimpleNamespace(), deps)

    expected = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    assert app.registrations == expected
    assert tuple(vars(installed)) == RETURNED_NAMES


def test_fleet_route_helpers_keep_the_per_install_job_roles_and_order():
    roles = {"develop": "dev", "verify": "verify", "intake": "intake_refiner", "master": "master"}
    helpers = routes.FleetRouteHelpers(SimpleNamespace(), roles)

    assert helpers.account_jobs(["intake_refiner", "dev"]) == ["develop", "intake"]
    assert helpers.JOB_ROLES is roles


def test_operation_plan_component_selection_keeps_repair_and_install_rules():
    repair = SimpleNamespace(action="repair", component="")
    install = SimpleNamespace(action="install", component="codex")

    assert routes._operation_components(repair, ["codex", "safe-container-runtime"], set(), ["curl"]) == [
        "codex", "packages",
    ]
    assert routes._operation_components(install, ["codex"], {"codex"}, []) == ["codex"]


def test_operator_intervention_route_is_registered_once_on_assembled_app(ctx):
    matches = [route for route in concrete_routes(create_app(ctx).routes)
               if route.path == "/v1/nodes/{name}/operator-interventions" and "POST" in route.methods]

    assert len(matches) == 1
    assert matches[0].endpoint.__name__ == "report_operator_intervention"


def test_operator_intervention_route_records_valid_and_duplicate_requests(client, ctx):
    joined = join_node(client, "cc")
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    body = {"source_key": "test:1", "source": "shell", "actor": "owner", "action": "execute",
            "details": {"node": "body-node", "extra": "retained"}}

    response = client.post("/v1/nodes/cc/operator-interventions", headers=headers, json=body)

    assert response.status_code == 202
    assert response.json()["recorded"] is True
    intervention_id = response.json()["id"]
    row = ctx.db.one("SELECT id,source_key,details FROM operator_interventions WHERE id=%s", (intervention_id,))
    assert row == {"id": intervention_id, "source_key": "host:cc:test:1",
                   "details": {"node": "body-node", "extra": "retained"}}

    duplicate = client.post("/v1/nodes/cc/operator-interventions", headers=headers,
                            json={**body, "actor": "duplicate"})

    assert duplicate.status_code == 202
    assert duplicate.json() == {"recorded": True, "id": intervention_id}
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions WHERE source_key='host:cc:test:1'") == {
        "n": 1,
    }


def test_operator_intervention_route_keeps_auth_and_body_errors(client, ctx):
    own = join_node(client, "cc")
    other = join_node(client, "other")
    own_headers = {"Authorization": f"Bearer {own.json()['node_token']}"}
    other_headers = {"Authorization": f"Bearer {other.json()['node_token']}"}
    path = "/v1/nodes/cc/operator-interventions"
    valid_body = {"source_key": "test:auth", "source": "shell", "actor": "owner", "action": "execute"}

    assert client.post(path, json=valid_body).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer invalid"}, json=valid_body).status_code == 401
    wrong_node = client.post(path, headers=other_headers, json=valid_body)
    assert wrong_node.status_code == 403
    assert wrong_node.json()["detail"] == "token belongs to another node"
    invalid_body = client.post(path, headers=own_headers, json={"source": "shell", "actor": "owner",
                                                               "action": "execute"})
    assert invalid_body.status_code == 422
    assert ctx.db.one("SELECT count(*) AS n FROM operator_interventions") == {"n": 0}
