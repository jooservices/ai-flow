from __future__ import annotations

import json

import pytest

from aiflow.v4.worker.client import ControlClient
from aiflow.v4.worker.loop import Worker


class Response:
    def __init__(self, status_code: int, body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.text = text or (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()
        self._body = body

    def json(self) -> dict:
        return self._body


class OldControlAPI:
    """Model an older control release: it has work routes but not version-release discovery."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, *, json=None, params=None) -> Response:
        self.requests.append((method, path, params or {}))
        if path == "/v1/version-releases/candidates":
            return Response(404, text="404 page not found")
        if path == "/v1/ba-chat/candidate":
            return Response(200, {"job": None})
        if path == "/v1/master/candidate":
            return Response(200, {"candidate": None})
        if path == "/v1/work/candidates":
            return Response(200, {"tickets": [{"key": "TESTWP-1", "step": "dev"}]})
        if method == "POST" and path == "/v1/work/claim":
            return Response(200, {
                "claimed": True,
                "run_id": "run-1",
                "fence": 1,
                "run_token": "run-token",
                "provider": "claude",
                "ticket": {"key": "TESTWP-1"},
                "project": {"key": "TESTWP"},
                "identity": [],
            })
        if method == "POST" and path in {"/v1/runs/run-1/result", "/v1/runs/run-1/release"}:
            return Response(200, {})
        return Response(404, text="404 page not found")


def test_worker_ahead_of_control_skips_missing_optional_route_and_completes_existing_work(tmp_path, monkeypatch):
    from aiflow.v4.worker import loop as worker_loop

    monkeypatch.setattr(worker_loop, "run_dev", lambda *_args: {"outcome": "done"})
    worker = Worker("claude-01", "unused", "unused", "cc", str(tmp_path), run_as=[])
    old_control = OldControlAPI()
    worker.api.c.close()
    worker.api.c = old_control
    worker.next_probe = float("inf")
    worker.probe_ok = True

    assert worker.once() is True

    assert worker.probe_ok is True
    assert worker.state["state"] == "idle" and worker.state["last_outcome"] == "done"
    paths = [path for _, path, _ in old_control.requests]
    assert paths == [
        "/v1/version-releases/candidates",
        "/v1/ba-chat/candidate",
        "/v1/master/candidate",
        "/v1/work/candidates",
        "/v1/work/claim",
        "/v1/runs/run-1/result",
        "/v1/runs/run-1/release",
    ]
    candidate_request = next(request for request in old_control.requests if request[1] == "/v1/work/candidates")
    assert candidate_request[2] == {"account": "claude-01", "worker_id": "claude-01"}


def test_control_client_only_accepts_404_when_route_is_marked_optional():
    api = object.__new__(ControlClient)
    api.c = OldControlAPI()

    assert api.call("GET", "/v1/version-releases/candidates", allow_not_found=True) is None
    with pytest.raises(RuntimeError, match="control API unreachable"):
        api.call("GET", "/v1/required", retries=1)
