"""Enforce the v4 package boundaries and the current feature registration contract."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "docs/v4/architecture.md"


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _imported_modules(path: Path) -> set[str]:
    """Resolve absolute and relative Python imports, including names in ``from . import module``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_name = _module_name(path)
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            if base:
                found.add(base)
            found.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return found


def _source_files(directory: Path):
    return sorted(directory.rglob("*.py"))


def _top_level_registration_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"install", "mount", "install_login"}}


def _create_app_registrations(path: Path) -> set[tuple[str | None, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    create_app = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_app")
    calls = set()
    for node in ast.walk(create_app):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            calls.add((node.func.value.id, node.func.attr))
        elif isinstance(node.func, ast.Name):
            calls.add((None, node.func.id))
    return calls


def test_control_and_core_keep_the_documented_dependency_direction():
    control = ROOT / "aiflow/v4/control"
    core = ROOT / "aiflow/v4/core"
    violations = []
    for path in _source_files(control):
        for imported in _imported_modules(path):
            if imported.startswith(("aiflow.v4.worker", "aiflow.v4.node")):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    for path in _source_files(core):
        # This module is the build-time docs command. It inspects the control app to emit generated API docs, but no
        # control runtime imports it except for serving the already-built static site.
        if path == core / "docsite.py":
            continue
        for imported in _imported_modules(path):
            if imported.startswith("aiflow.v4.control"):
                violations.append(f"{path.relative_to(ROOT)} imports {imported}")
    assert not violations, "\n".join(violations)


def test_feature_route_installers_are_registered_in_create_app():
    """The current extraction bridge keeps explicit installers reachable from the app entry point."""
    control = ROOT / "aiflow/v4/control"
    entry_point = control / "api.py"
    calls = _create_app_registrations(entry_point)
    missing = []
    for path in _source_files(control):
        if path == entry_point:
            continue
        for function in _top_level_registration_functions(path):
            target = (None, function) if function == "install_login" else (path.stem, function)
            if target not in calls:
                missing.append(f"{path.relative_to(control)}:{function}")
    assert not missing, f"feature route installers must be registered from create_app: {sorted(set(missing))}"


def test_architecture_contract_and_readme_registration_are_present():
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    topics = (ROOT / "deploy/v4/README.md").read_text(encoding="utf-8").split("## Topics", 1)[1]
    for requirement in (
        "## Layers and dependencies", "Routes", "Services", "Data access", "Adapters", "Core",
        "control must not import", "Runtime core modules must not import control", "## Patterns and registration points",
        "chain of responsibility", "Strategy", "APIRouter", "discovery", "provider pattern",
        "table-driven state machine", "tickets, runs, accounts, nodes and incidents", "get_adapter",
        "## Principles as checks", "Single responsibility", "DRY", "KISS", "YAGNI",
        "80 lines", "complexity 12", "caller or a test tied to a requirement", "dead generations",
    ):
        assert requirement.casefold() in architecture.casefold(), f"architecture is missing {requirement!r}"
    assert "[v4 control plane architecture](../../docs/v4/architecture.md)" in topics


def test_verify_run_repository_keeps_ticket_node_and_incident_queries_in_their_repositories():
    verify_runs = (ROOT / "aiflow/v4/control/repositories/verify_runs.py").read_text(encoding="utf-8")
    assert not any(table in verify_runs for table in ("FROM tickets", "FROM nodes", "FROM incidents"))
    tickets = (ROOT / "aiflow/v4/control/repositories/tickets.py").read_text(encoding="utf-8")
    nodes = (ROOT / "aiflow/v4/control/repositories/nodes.py").read_text(encoding="utf-8")
    incidents = (ROOT / "aiflow/v4/control/repositories/incidents.py").read_text(encoding="utf-8")
    assert "def verification_candidates" in tickets and "def waiting_verifications" in tickets
    assert "def candidate_node" in nodes
    assert "def open_subjects" in incidents


def test_work_candidate_sql_queries_live_in_ticket_and_run_repositories():
    source = (ROOT / "aiflow/v4/control/work_candidates.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    inline_selects = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"all", "one"} or not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        query = node.args[0].value
        if isinstance(query, str) and query.lstrip().upper().startswith("SELECT"):
            inline_selects.append(query.strip().splitlines()[0])
    assert not inline_selects, f"candidate service contains SQL: {inline_selects}"
    tickets = (ROOT / "aiflow/v4/control/repositories/tickets.py").read_text(encoding="utf-8")
    runs = (ROOT / "aiflow/v4/control/repositories/runs.py").read_text(encoding="utf-8")
    for method in ("by_key", "triage_candidates", "intake_candidates", "impact_candidates",
                   "development_candidates", "done_keys"):
        assert f"def {method}" in tickets
        assert f".{method}(" in source
    assert ".running_counts_by_project()" in source


def test_capacity_routing_reads_account_and_run_history_through_repositories():
    capacity_path = ROOT / "aiflow/v4/control/capacity.py"
    capacity = capacity_path.read_text(encoding="utf-8")
    tree = ast.parse(capacity, filename=str(capacity_path))
    route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "route_candidates")
    inline_queries = [node.args[0].value for node in ast.walk(route)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in {"all", "one"} and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.lstrip().upper().startswith("SELECT")]
    accounts = (ROOT / "aiflow/v4/control/repositories/accounts.py").read_text(encoding="utf-8")
    runs = (ROOT / "aiflow/v4/control/repositories/runs.py").read_text(encoding="utf-8")
    incidents = (ROOT / "aiflow/v4/control/incidents.py").read_text(encoding="utf-8")

    assert not inline_queries
    assert "AccountsRepository(ctx.db).capacity_routing_accounts()" in capacity
    assert "def capacity_routing_accounts" in accounts
    assert "def capacity_history" in runs
    assert "RunsRepository(db).capacity_history(project_keys)" in capacity


def test_capacity_dashboard_and_route_queries_live_in_aggregate_repositories():
    capacity_path = ROOT / "aiflow/v4/control/capacity.py"
    capacity_tree = ast.parse(capacity_path.read_text(encoding="utf-8"), filename=str(capacity_path))
    dashboard = next(node for node in capacity_tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "dashboard")
    inline_queries = [node.args[0].value for node in ast.walk(dashboard)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in {"all", "one"} and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.lstrip().upper().startswith("SELECT")]
    api_path = ROOT / "aiflow/v4/control/api.py"
    api_tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
    capacity_route = next(node for node in ast.walk(api_tree) if isinstance(node, ast.FunctionDef)
                          and node.name == "capacity_plan")
    route_queries = [node.args[0].value for node in ast.walk(capacity_route)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr in {"all", "one"} and node.args
                     and isinstance(node.args[0], ast.Constant)
                     and isinstance(node.args[0].value, str)
                     and node.args[0].value.lstrip().upper().startswith("SELECT")]
    assert not inline_queries and not route_queries
    tickets = (ROOT / "aiflow/v4/control/repositories/tickets.py").read_text(encoding="utf-8")
    runs = (ROOT / "aiflow/v4/control/repositories/runs.py").read_text(encoding="utf-8")
    accounts = (ROOT / "aiflow/v4/control/repositories/accounts.py").read_text(encoding="utf-8")
    assert "TicketsRepository(db).capacity_ready_tickets(project_keys)" in capacity_path.read_text()
    assert "def capacity_ready_tickets" in tickets
    assert "def capacity_decisions" in runs and "def capacity_estimate_rows" in runs
    assert "def capacity_actual" in runs
    assert "AccountsRepository(ctx.db).capacity_dashboard_accounts()" in api_path.read_text()
    assert "def capacity_dashboard_accounts" in accounts


def test_idle_flow_ticket_queries_live_in_tickets_repository():
    incidents_path = ROOT / "aiflow/v4/control/incidents.py"
    incidents_tree = ast.parse(incidents_path.read_text(encoding="utf-8"), filename=str(incidents_path))
    services = [node for node in incidents_tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"workable_tickets", "idle_tickets"}]
    inline_queries = [query for service in services for node in ast.walk(service)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in {"all", "one"} and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.lstrip().upper().startswith("SELECT")
                      for query in [node.args[0].value]]
    tickets = (ROOT / "aiflow/v4/control/repositories/tickets.py").read_text(encoding="utf-8")
    assert not inline_queries
    assert "tickets = TicketsRepository(ctx.db)" in incidents_path.read_text()
    assert "tickets.idle_flow_candidates(" in incidents_path.read_text()
    assert "def idle_flow_candidates" in tickets


def test_candidate_setup_uses_aggregate_repositories_for_its_query_shapes():
    api_path = ROOT / "aiflow/v4/control/api.py"
    api_tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
    query_helpers = {node.name: node for node in ast.walk(api_tree)
                     if isinstance(node, ast.FunctionDef)
                     and node.name in {"eligible", "ticket_epic", "last_dev", "quota_forecast_at_risk",
                                       "quota_peer_accounts"}}
    inline_queries = {
        name: [node.args[0].value for node in ast.walk(function)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr in {"all", "one"} and node.args
               and isinstance(node.args[0], ast.Constant)
               and isinstance(node.args[0].value, str)
               and node.args[0].value.lstrip().upper().startswith("SELECT")]
        for name, function in query_helpers.items()
    }
    inline_queries = {name: queries for name, queries in inline_queries.items() if queries}
    api = api_path.read_text(encoding="utf-8")
    candidates = (ROOT / "aiflow/v4/control/work_candidates.py").read_text(encoding="utf-8")
    runtime = (ROOT / "aiflow/v4/control/candidate_runtime.py").read_text(encoding="utf-8")
    accounts = (ROOT / "aiflow/v4/control/repositories/accounts.py").read_text(encoding="utf-8")
    runs = (ROOT / "aiflow/v4/control/repositories/runs.py").read_text(encoding="utf-8")
    incidents = (ROOT / "aiflow/v4/control/incidents.py").read_text(encoding="utf-8")

    assert not inline_queries
    assert "NodesRepository(ctx.db).candidate_node(node)" in runtime
    assert "AccountsRepository(self.ctx.db).quota_at_risk(account_id)" in runtime
    assert "AccountsRepository(ctx.db).quota_peer_candidates(account_id)" in runtime
    assert "BACKOFF_SQL" not in api
    assert "RunsRepository(ctx.db).cooling_tickets(\"dev\")" in incidents
    assert "VerifyRunsRepository(ctx.db).cooling_tickets()" in incidents
    assert "runs_repo=RunsRepository(ctx.db)" in runtime
    assert "tickets_repo=TicketsRepository(ctx.db)" in runtime
    assert "initialize(ctx, RELEASES)" in api
    assert "state.runs_repo.latest_development" in candidates
    assert "state.runs_repo.cooling_tickets" in candidates
    assert "state.runs_repo.intake_waiting_tickets" in candidates
    for method in ("latest_development", "cooling_tickets", "intake_waiting_tickets"):
        assert f"def {method}" in runs
    for method in ("quota_at_risk", "quota_peer_candidates"):
        assert f"def {method}" in accounts


def test_state_snapshot_passes_release_configuration_and_uses_aggregate_repositories():
    state_path = ROOT / "aiflow/v4/control/state.py"
    state_tree = ast.parse(state_path.read_text(encoding="utf-8"), filename=str(state_path))
    inline_queries = [node.args[0].value for node in ast.walk(state_tree)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr in {"all", "one", "run"} and node.args
                      and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str)
                      and node.args[0].value.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))]
    reverse_api_imports = [node for node in ast.walk(state_tree) if isinstance(node, ast.ImportFrom)
                           and node.module == "api"]
    api = (ROOT / "aiflow/v4/control/api.py").read_text(encoding="utf-8")

    assert not inline_queries
    assert not reverse_api_imports
    assert "snapshot(ctx, RELEASES, include_signin=include_signin)" in api
    assert "return snapshot(ctx, RELEASES)" in api


def test_watchdog_is_split_into_functions_within_the_architecture_size_budget():
    loops_path = ROOT / "aiflow/v4/control/loops.py"
    tree = ast.parse(loops_path.read_text(encoding="utf-8"), filename=str(loops_path))
    checks = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
              and (node.name == "watchdog" or node.name.startswith("_watch_")
                   or node.name in {"_requeue_orphan_ticket", "_mark_lost_run", "_audit_lost_run",
                                    "_watch_quota_window", "_open_quota_prediction"})]

    oversized = {node.name: node.end_lineno - node.lineno + 1 for node in checks
                 if node.end_lineno - node.lineno + 1 > 80}
    assert not oversized, f"watchdog responsibilities exceed the 80-line limit: {oversized}"


def test_verify_worker_separates_setup_verdict_and_merge_within_architecture_budget():
    verify_path = ROOT / "aiflow/v4/worker/verify.py"
    tree = ast.parse(verify_path.read_text(encoding="utf-8"), filename=str(verify_path))
    expected = {"run_verify", "_prepare_verify_workspace", "_execute_review", "_handle_verdict",
                "_complete_pass", "_complete_merge"}
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    missing = expected - functions.keys()
    oversized = {name: functions[name].end_lineno - functions[name].lineno + 1
                 for name in expected & functions.keys()
                 if functions[name].end_lineno - functions[name].lineno + 1 > 80}

    assert not missing, f"verify stages are not extracted into focused helpers: {sorted(missing)}"
    assert not oversized, f"verify responsibilities exceed the 80-line limit: {oversized}"


def test_developer_and_verify_prompts_use_architecture_document():
    developer = (ROOT / "prompts/v4/dev.md").read_text(encoding="utf-8")
    verify = (ROOT / "prompts/v4/verify.md").read_text(encoding="utf-8")
    assert "Before changing code on every developer run, read `docs/v4/architecture.md`" in developer
    assert "Include the architecture document in" in developer and "run notes" in developer
    assert "Use `docs/v4/architecture.md` as the architecture checklist" in verify
