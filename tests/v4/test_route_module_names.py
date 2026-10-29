"""A route module may not use a name it never defines or imports (a moved handler once called a helper that stayed in api.py)."""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

ROUTES = sorted((Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "routes").glob("*.py"))


def undefined_names(source: str) -> list[str]:
    tree = ast.parse(source)
    defined = set(dir(builtins)) | {"__file__", "__name__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id not in defined})


def test_the_check_finds_an_undefined_name():
    assert undefined_names("def f():\n    return missing_helper(1)\n") == ["missing_helper"]
    assert undefined_names("import os\ndef f(x):\n    return os.path.join(x)\n") == []


def test_every_route_module_defines_or_imports_every_name_it_uses():
    problems = {p.name: undefined_names(p.read_text(encoding="utf-8")) for p in ROUTES}
    assert {k: v for k, v in problems.items() if v} == {}


def test_concrete_routes_look_inside_included_routers():
    from fastapi import APIRouter, FastAPI

    from aiflow.v4.control.route_registry import concrete_routes

    app = FastAPI()
    plain, nested = APIRouter(), APIRouter()

    @plain.get("/a")
    def a():
        return 1

    @nested.get("/b")
    def b():
        return 2

    app.include_router(plain)
    app.include_router(nested, prefix="/v1")

    @app.get("/c")
    def c():
        return 3

    paths = sorted(getattr(r, "path", "") for r in concrete_routes(app.routes) if getattr(r, "path", "").startswith(("/a", "/v1/b", "/c")))
    assert paths == ["/a", "/c", "/v1/b"]
