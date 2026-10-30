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


def _complexity(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1

    def visit(node):
        nonlocal score
        if node is not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp,
                             ast.comprehension, ast.Match, ast.match_case)):
            score += 1
        if isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(function)
    return score


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
            if path.name == "routes" + ".py" and path.parent.parent.name == "features":
                target = (f"{path.parent.name}_module", function)  # features/<name>/routes.py is imported as <name>_module
            else:
                target = (None, function) if function == "install_login" else (path.stem, function)
            if target not in calls:
                missing.append(f"{path.relative_to(control)}:{function}")
    assert not missing, f"feature route installers must be registered from create_app: {sorted(set(missing))}"


def test_provider_routing_routes_delegate_orchestration_to_service():
    route = (ROOT / "aiflow/v4/control/routes/provider_routing.py").read_text(encoding="utf-8")
    assert "ctx.provider_routing" in route
    for forbidden in ("ProviderRoutingRepository", "AccountRepository", "provider_routing.evaluate",
                      "provider_routing.set_settings"):
        assert forbidden not in route


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


def test_developer_and_verify_prompts_use_architecture_document():
    developer = (ROOT / "prompts/v4/dev.md").read_text(encoding="utf-8")
    verify = (ROOT / "prompts/v4/verify.md").read_text(encoding="utf-8")
    assert "Before changing code on every developer run, read `docs/v4/architecture.md`" in developer
    assert "Include the architecture document in" in developer and "run notes" in developer
    assert "Use `docs/v4/architecture.md` as the architecture checklist" in verify


def test_resume_waiting_stays_within_architecture_budgets():
    module = ast.parse((ROOT / "aiflow/v4/control/escalations.py").read_text(encoding="utf-8"))
    function = next(node for node in ast.walk(module)
                    if isinstance(node, ast.FunctionDef) and node.name == "resume_waiting")
    assert function.end_lineno - function.lineno + 1 <= 80
    assert _complexity(function) <= 12


def test_owner_question_formatter_and_jira_gateway_stay_within_complexity_budget():
    core = ast.parse((ROOT / "aiflow/v4/core/owner_escalation.py").read_text(encoding="utf-8"))
    formatter = next(node for node in core.body if isinstance(node, ast.FunctionDef)
                     and node.name == "owner_question_comment")
    jira = ast.parse((ROOT / "aiflow/v4/control/jira.py").read_text(encoding="utf-8"))
    gateway = next(node for node in jira.body if isinstance(node, ast.ClassDef) and node.name == "Jira")
    call = next(node for node in gateway.body if isinstance(node, ast.FunctionDef) and node.name == "call")

    violations = [f"{function.name}: complexity {_complexity(function)}"
                  for function in (formatter, call) if _complexity(function) > 12]
    assert not violations, "owner escalation functions exceed the complexity budget: " + "; ".join(violations)


def test_event_architecture_document_states_the_structure_and_is_linked():
    doc = (ROOT / "docs/v4/event-architecture.md").read_text(encoding="utf-8").casefold()
    for requirement in ("control/features/<name>/", "routes.py", "service.py", "repository.py", "models.py", "events.py",
                        "listeners.py", "past tense", "outbox", "in_transaction", "idempotent", "walking skeleton",
                        "claim path stays one atomic synchronous transaction", "adr-1", "no event sourcing"):
        assert requirement.casefold() in doc, f"event architecture is missing {requirement!r}"
    assert "event-architecture.md" in ARCHITECTURE.read_text(encoding="utf-8")
    assert (ROOT / "deploy/v4/topics/event-architecture.md").exists()
