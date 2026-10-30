"""The import rules of docs/v4/event-architecture.md for `control/features/<name>/` (JAIF-455).

No feature exists yet, so the real-tree test passes on an empty folder; the synthetic trees prove each rule fires."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "aiflow/v4/control/features"
PRIVATE_PARTS = {"service", "repository", "routes"}  # another feature may import only `events` and `models`
NO_WEB = {"service", "repository", "models", "events", "listeners"}  # parts that must not know about FastAPI


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            found.add(base)
            found.update(f"{base}.{alias.name}".replace("..", ".") if base.endswith(".") else f"{base}.{alias.name}"
                         for alias in node.names)
    return found


def feature_violations(features: Path) -> list[str]:
    problems: list[str] = []
    if not features.is_dir():
        return sorted(set(problems))
    for feature in sorted(p for p in features.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for path in sorted(feature.rglob("*.py")):
            part = path.stem
            where = f"{feature.name}/{path.name}"
            for imported in _imports(path):
                pieces = [p for p in imported.replace("..", ".").split(".") if p]
                if "features" in pieces:
                    rest = pieces[pieces.index("features") + 1:]
                    if len(rest) >= 2 and rest[0] != feature.name and rest[1] in PRIVATE_PARTS:
                        problems.append(f"{where} imports {rest[1]} of feature {rest[0]}; use its events or models")
                if part in NO_WEB and pieces[:1] == ["fastapi"]:
                    problems.append(f"{where} imports fastapi; only routes.py may")
                if part == "models" and pieces[:2] == ["aiflow", "v4"] and "core" not in pieces[:3]:
                    problems.append(f"{where} imports {imported}; models depend on nothing of control")
                if part == "routes" and pieces and pieces[-1] == "repository" and imported.startswith("."):
                    problems.append(f"{where} imports its repository; a route calls its service")
    return sorted(set(problems))


def test_real_features_follow_the_import_rules():
    assert feature_violations(FEATURES) == []


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return tmp_path


def test_a_feature_may_not_import_another_features_service(tmp_path):
    root = _tree(tmp_path, {"tickets/service.py": "from aiflow.v4.control.features.fleet.service import x\n",
                            "fleet/service.py": "x = 1\n"})
    assert any("service of feature fleet" in line for line in feature_violations(root))


def test_a_feature_may_listen_to_another_features_events_and_read_its_models(tmp_path):
    root = _tree(tmp_path, {"tickets/listeners.py": "from aiflow.v4.control.features.fleet.events import NodeJoined\n"
                                                    "from aiflow.v4.control.features.fleet.models import Node\n",
                            "fleet/events.py": "", "fleet/models.py": ""})
    assert feature_violations(root) == []


def test_only_routes_may_import_fastapi(tmp_path):
    root = _tree(tmp_path, {"tickets/service.py": "from fastapi import HTTPException\n",
                            "tickets/routes.py": "from fastapi import APIRouter\n"})
    assert [line for line in feature_violations(root) if "fastapi" in line] == [
        "tickets/service.py imports fastapi; only routes.py may"]


def test_models_depend_on_nothing_of_control(tmp_path):
    root = _tree(tmp_path, {"tickets/models.py": "from aiflow.v4.control.db import DB\n"})
    assert any("models depend on nothing" in line for line in feature_violations(root))


def test_a_route_calls_its_service_not_its_repository(tmp_path):
    root = _tree(tmp_path, {"tickets/routes.py": "from . import repository\n", "tickets/repository.py": ""})
    assert any("route calls its service" in line for line in feature_violations(root))
