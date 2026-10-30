"""Golden response shapes of the key read routes (JAIF-457).

The refactor of epic JAIF-445 moves code under these routes; each must keep its status and the shape of its JSON
(keys and value types, not values). Regenerate after an intended change: `AIFLOW_WRITE_GOLDEN=1 uv run pytest
tests/v4/test_golden_responses.py`, and review the diff of the fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "fixtures" / "golden-response-shapes.json"
OWNER = {"Authorization": "Bearer owner-secret"}
ROUTES = (
    "/healthz", "/v1/analytics/conflicts", "/v1/analytics/daily", "/v1/analytics/usage", "/v1/analytics/usage/daily",
    "/v1/capacity", "/v1/console/session", "/v1/dashboard/disagreements", "/v1/dashboard/quota",
    "/v1/dashboard/timeouts", "/v1/dashboard/waiting", "/v1/deploy/state", "/v1/environment/missing",
    "/v1/memory/search", "/v1/memory/study-cases", "/v1/projects", "/v1/provider-routing",
    "/v1/provider-routing/decisions", "/v1/reviews", "/v1/role-health", "/v1/scale-proof", "/v1/setup/status",
    "/v1/state", "/v1/support-bundle", "/v1/system-check",
)


def shape(value):
    """Keys and value types of a JSON value; a list is described by its first element."""
    if isinstance(value, dict):
        return {key: shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return "null" if value is None else type(value).__name__


def _observed(client) -> dict:
    out = {}
    for path in ROUTES:
        response = client.get(path, headers=OWNER)
        out[path] = {"status": response.status_code, "shape": shape(response.json())}
    return out


def test_key_read_routes_keep_their_status_and_response_shape(client):
    observed = _observed(client)
    if os.environ.get("AIFLOW_WRITE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(observed, indent=1, sort_keys=True) + "\n")
        pytest.skip("golden response shapes rewritten")
    expected = json.loads(GOLDEN.read_text())
    assert set(observed) == set(expected)
    changed = {path: {"expected": expected[path], "got": observed[path]}
               for path in expected if observed[path] != expected[path]}
    assert not changed, json.dumps(changed, indent=1, sort_keys=True)[:4000]


def test_shape_describes_keys_and_types_only():
    assert shape({"b": [1, 2], "a": {"x": None, "y": "s", "z": 1.5}, "c": []}) == {
        "a": {"x": "null", "y": "str", "z": "float"}, "b": ["int"], "c": []}
