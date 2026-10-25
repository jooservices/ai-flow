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
    assert "def running_counts_by_project" in runs
    assert ".running_counts_by_project()" in source


def test_developer_and_verify_prompts_use_architecture_document():
    developer = (ROOT / "prompts/v4/dev.md").read_text(encoding="utf-8")
    verify = (ROOT / "prompts/v4/verify.md").read_text(encoding="utf-8")
    assert "Before changing code on every developer run, read `docs/v4/architecture.md`" in developer
    assert "Include the architecture document in" in developer and "run notes" in developer
    assert "Use `docs/v4/architecture.md` as the architecture checklist" in verify
