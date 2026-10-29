"""The control API audit against the company HTTP standard (JAIF-414)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from aiflow.v4.control import api, rest_audit as audit

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "v4" / "api-rest-audit.md"


def test_findings_follow_the_standard():
    f = audit.findings_for
    assert f("GET", "/v1/runs/{run_id}") == []
    assert f("POST", "/v1/projects") == []
    assert f("POST", "/v1/projects/{key}/pause") == [audit.OPERATION_OK]
    assert any("verb in path" in x for x in f("POST", "/v1/nodes/join"))
    assert any("unsafe or verb GET" in x for x in f("GET", "/v1/environment/scan"))
    assert any("not plural" in x for x in f("GET", "/v1/dashboard/{id}"))
    assert any("no version prefix" in x for x in f("GET", "/other"))
    assert f("GET", "/healthz") == []


def test_clients_are_detected_from_the_worker_and_node_sources():
    sources = '"/v1/nodes/{node}/operations/next" and f"/v1/runs/{c.run_id}/release"'
    assert audit.used_by_clients("/v1/nodes/{name}/operations/next", sources)
    assert audit.used_by_clients("/v1/runs/{run_id}/release", sources)
    assert not audit.used_by_clients("/v1/projects", sources)


def test_every_route_of_the_app_is_in_the_committed_audit(ctx):
    rows = audit.audit(api.create_app(ctx).routes, ROOT)
    expected = audit.render(rows)
    if os.environ.get("AIFLOW_WRITE_AUDIT"):
        DOC.write_text(expected, encoding="utf-8")
    assert DOC.read_text(encoding="utf-8") == expected, (
        "a route was added or changed: run `AIFLOW_WRITE_AUDIT=1 uv run pytest tests/v4/test_rest_audit.py` and review "
        "the new rows against the company HTTP standard")
    assert len(rows) > 200


def test_global_findings_are_still_true(client):
    missing = client.get("/v1/this-route-does-not-exist")
    assert missing.status_code == 404
    assert "application/problem+json" not in missing.headers.get("content-type", "")  # G2
    assert set(missing.json()) == {"detail"}
