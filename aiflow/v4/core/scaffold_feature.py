"""`python3 -m aiflow.v4.core.scaffold_feature <name>`: create a control-plane feature folder from the templates of
docs/v4/event-architecture.md, with its route registration and a first test. It only writes new files."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

TEMPLATES = {
    "__init__.py": '"""{title} feature: see docs/v4/event-architecture.md."""\n',
    "models.py": '"""Entities of the {title} feature: frozen dataclasses, no imports from control."""\nfrom __future__ import annotations\n',
    "events.py": '"""Domain events published by the {title} feature: past-tense facts, frozen dataclasses."""\nfrom __future__ import annotations\n',
    "repository.py": '"""All SQL of the {title} aggregate; returns the dataclasses of models.py, never raw dicts."""\nfrom __future__ import annotations\n',
    "service.py": '"""Use cases of the {title} feature. No FastAPI, no HTTPException: raise domain errors."""\nfrom __future__ import annotations\n',
    "listeners.py": '"""Reactions of the {title} feature to domain events: idempotent `@on(Event)` functions."""\nfrom __future__ import annotations\n',
    "routes.py": '"""HTTP routes of the {title} feature: validate, authorise, call one service method."""\nfrom __future__ import annotations\n\nfrom fastapi import APIRouter\n\nrouter = APIRouter()\n',
}
ROUTE_SHIM = ('"""Mounts the {title} feature router through the route registry (control/routes/)."""\n'
              "from ..features.{name}.routes import router  # noqa: F401\n")
TEST = ('"""First tests of the {title} feature."""\nfrom aiflow.v4.control.features.{name} import routes\n\n\n'
        "def test_the_{name}_router_is_mounted_by_the_registry():\n"
        "    from aiflow.v4.control.routes import {name} as mounted\n\n    assert mounted.router is routes.router\n")


def scaffold(root: Path, name: str) -> list[Path]:
    """Write the feature under `root` (the repository root) and return the files created."""
    if not NAME.match(name):
        raise ValueError("a feature name is lower-case letters, digits and underscores, 2 to 31 characters")
    control = root / "aiflow/v4/control"
    folder = control / "features" / name
    targets = {folder / file: text for file, text in TEMPLATES.items()}
    targets[control / "routes" / f"{name}.py"] = ROUTE_SHIM
    targets[root / "tests/v4/features" / f"test_{name}.py"] = TEST
    clash = [str(path.relative_to(root)) for path in targets if path.exists()]
    if clash:
        raise FileExistsError("refusing to overwrite: " + ", ".join(clash))
    title = name.replace("_", " ")
    created = []
    packages = [control / "features" / "__init__.py", root / "tests/v4/features/__init__.py"]
    for package in packages:
        if not package.exists():
            package.parent.mkdir(parents=True, exist_ok=True)
            package.write_text("")
            created.append(package)
    for path, text in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.format(name=name, title=title.capitalize()))
        created.append(path)
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        created = scaffold(args.root.resolve(), args.name)
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in created:
        print(path.relative_to(args.root.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
