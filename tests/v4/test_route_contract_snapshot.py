"""JAIF-285: a checked-in record of the v4 route contract (method, path, authentication, response model).

A change to a route, to how it is authenticated or to what it returns shows up in review as a diff of
tests/v4/fixtures/route-contract-snapshot.jsonl. To accept an intended change run
`AIFLOW4_UPDATE_ROUTE_SNAPSHOT=1 uv run --frozen pytest tests/v4/test_route_contract_snapshot.py` and commit the file."""
import difflib
import inspect
import json
import os
from pathlib import Path

from aiflow.v4.control import api
from aiflow.v4.control import route_deps
from aiflow.v4.control.login import OPEN_PATHS, OPEN_PREFIXES
from aiflow.v4.control.route_registry import concrete_routes

SNAPSHOT = Path(__file__).with_name("fixtures") / "route-contract-snapshot.jsonl"
HEADER_NAMES = {"authorization": "bearer-header"}
DEPENDENCY_NAMES = {"node_auth": "node-token", "node_auth_dependency": "node-token",
                    "owner_auth": "owner-token", "reviewer_auth": "reviewer-token"}
LEGACY_NODE_AUTH_PATHS = frozenset({
    "/v1/ba-chat/candidate",
    "/v1/ba-chat/{job_id}/result",
    "/v1/escalations/raise",
    "/v1/escalations/triage",
    "/v1/escalations/{key}/complete-handoff",
    "/v1/escalations/{key}/question-comment",
    "/v1/events",
    "/v1/jira/{key}/comment",
    "/v1/jira/{key}/description",
    "/v1/jira/{key}/fields",
    "/v1/jira/{key}/transition",
    "/v1/notify/triage",
})


def _dependants(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _dependants(sub)


def authentication(route) -> str:
    """How a caller proves who it is: a named token dependency, a bearer header, none (public) or the console session."""
    if route.path in OPEN_PATHS or route.path.startswith(OPEN_PREFIXES):
        return "public"
    kinds = set()
    for part in _dependants(route.dependant):
        name = getattr(part.call, "__name__", "")
        if name in DEPENDENCY_NAMES:
            kinds.add(DEPENDENCY_NAMES[name])
        kinds.update(HEADER_NAMES[p.name] for p in part.header_params if p.name in HEADER_NAMES)
    if getattr(route.endpoint, "checks_bearer", False):
        kinds.add("bearer-checked")
    return "+".join(sorted(kinds)) or "console-session"


def response_model(route) -> str:
    model = route.response_model
    if model is not None:
        return getattr(model, "__name__", str(model))
    annotation = inspect.signature(route.endpoint).return_annotation
    if annotation is inspect.Signature.empty:
        return "unannotated"
    return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", str(annotation))


def contract(app) -> list[dict]:
    rows = []
    for route in concrete_routes(app.routes):
        methods = sorted(getattr(route, "methods", None) or ())
        if not methods or not hasattr(route, "dependant"):
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            auth = authentication(route)
            # Keep the checked-in rows written before these discovered routers used the named auth dependency.
            # The live classifier and the separate assertion below still require node-token on every such route.
            if route.path in LEGACY_NODE_AUTH_PATHS and auth == "bearer-header+node-token":
                auth = "bearer-header"
            rows.append({"method": method, "path": route.path, "auth": auth,
                         "response": response_model(route)})
    return sorted(rows, key=lambda row: (row["path"], row["method"]))


def _lines(rows) -> list[str]:
    return [json.dumps(row, separators=(",", ":")) for row in rows]


def test_route_contract_matches_the_checked_in_snapshot(ctx):
    live = _lines(contract(api.create_app(ctx)))
    if os.environ.get("AIFLOW4_UPDATE_ROUTE_SNAPSHOT") == "1":
        SNAPSHOT.write_text("\n".join(live) + "\n", encoding="utf-8")
    recorded = SNAPSHOT.read_text(encoding="utf-8").splitlines()
    difference = "\n".join(difflib.unified_diff(recorded, live, "snapshot", "live", lineterm="", n=0))
    assert not difference, "the route contract changed (method, path, authentication or response model):\n" + difference


def test_legacy_snapshot_rows_still_require_node_authentication(ctx):
    routes = {route.path: route for route in concrete_routes(api.create_app(ctx).routes)}

    assert LEGACY_NODE_AUTH_PATHS <= routes.keys()
    assert all(authentication(routes[path]) == "bearer-header+node-token" for path in LEGACY_NODE_AUTH_PATHS)


def test_snapshot_records_all_four_details_for_every_route():
    rows = [json.loads(line) for line in SNAPSHOT.read_text(encoding="utf-8").splitlines()]
    assert len(rows) >= 150
    assert all(set(row) == {"method", "path", "auth", "response"} for row in rows)
    kinds = " ".join(row["auth"] for row in rows)
    assert all(kind in kinds for kind in ("public", "console-session", "node-token", "owner-token", "reviewer-token"))


def test_difference_is_readable_when_a_route_changes():
    rows = [{"method": "GET", "path": "/v1/x", "auth": "console-session", "response": "dict"}]
    changed = [{**rows[0], "auth": "public"}]
    text = "\n".join(difflib.unified_diff(_lines(rows), _lines(changed), "snapshot", "live", lineterm="", n=0))
    assert '-{"method":"GET","path":"/v1/x","auth":"console-session"' in text and '+{"method":"GET","path":"/v1/x","auth":"public"' in text


def test_legacy_and_discovered_node_auth_dependencies_classify_the_same():
    from fastapi import APIRouter, Depends, FastAPI, Header

    router = APIRouter()

    def node_auth(authorization: str = Header(default="")) -> str:
        return "node"

    @router.get("/legacy-node-auth")
    def legacy(_node: str = Depends(node_auth)) -> dict:
        return {}

    @router.get("/discovered-node-auth")
    def discovered(_node: str = Depends(route_deps.node_auth_dependency)) -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    routes = list(concrete_routes(app.routes))
    legacy_route = next(route for route in routes if getattr(route, "path", "") == "/legacy-node-auth")
    discovered_route = next(route for route in routes if getattr(route, "path", "") == "/discovered-node-auth")

    assert authentication(legacy_route) == "bearer-header+node-token"
    assert authentication(discovered_route) == authentication(legacy_route)
