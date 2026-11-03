"""The route registrations of every feature `install` function, recorded once (JAIF-547 to 558).

Each feature's `install(app, ctx, deps)` runs against a recording fake app with mock dependencies; every registration
(method, path, handler name, handler signature, decorator options) is compared with
tests/v4/fixtures/route-install-registrations.jsonl. A split of an install function into route groups must keep this
list identical (same routes, same order). To accept an intended change run
`AIFLOW4_UPDATE_ROUTE_SNAPSHOT=1 uv run --frozen pytest tests/v4/test_route_install_registrations.py` and commit the file."""
import importlib
import inspect
import json
import os
import pathlib
import re
from unittest.mock import MagicMock

import pytest

FEATURES = pathlib.Path(__file__).parents[2] / "aiflow/v4/control/features"
GOLDEN = pathlib.Path(__file__).with_name("fixtures") / "route-install-registrations.jsonl"
NAMES = sorted(path.parent.name for path in FEATURES.glob("*/routes.py"))


class RecordingApp:
    """Stands in for FastAPI: each route decorator records its registration and returns the handler unchanged."""

    def __init__(self):
        self.registered = []

    def __getattr__(self, method):
        def factory(path="", *args, **options):
            def decorator(handler):
                self.registered.append({
                    "method": method.upper(), "path": path, "handler": handler.__name__,
                    "signature": _plain(str(inspect.signature(handler))),
                    "options": json.loads(json.dumps(options, default=lambda value: _plain(repr(value))))})
                return handler
            return decorator
        return factory


def _plain(text: str) -> str:
    """Drop memory addresses and mock ids, which change from run to run."""
    return re.sub(r"id='\d+'", "id=N", re.sub(r"0x[0-9a-f]+", "0x", text))


def record(feature: str) -> list[dict]:
    module = importlib.import_module(f"aiflow.v4.control.features.{feature}.routes")
    app = RecordingApp()
    module.install(app, MagicMock(), MagicMock())
    return [{"feature": feature, **item} for item in app.registered]


def _golden() -> dict[str, list[dict]]:
    rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {name: [row for row in rows if row["feature"] == name] for name in NAMES}


@pytest.fixture(scope="module", autouse=True)
def maybe_update():
    if os.environ.get("AIFLOW4_UPDATE_ROUTE_SNAPSHOT") == "1":
        rows = [row for name in NAMES for row in record(name)]
        GOLDEN.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")


@pytest.mark.parametrize("feature", NAMES)
def test_registrations_match_the_recorded_list(feature):
    assert record(feature) == _golden()[feature]


def test_every_feature_registers_routes():
    golden = _golden()
    assert all(golden[name] for name in NAMES)
    assert sum(len(rows) for rows in golden.values()) >= 100
