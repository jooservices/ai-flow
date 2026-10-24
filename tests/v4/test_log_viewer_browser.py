"""JAIF-175: the /logs page's client-side flow in a real browser, with a fake node agent completing the reads."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest

from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import mount_ui
from conftest import join_node

SUITE = Path(__file__).with_name("log_viewer_browser_suite.cjs")
SESSION_SECRET = "log-viewer-browser-secret-with-at-least-32-bytes"
NODE = "log-browser-node"
LEAKED_VALUE = "browser-sample-secret-value"


def fake_node_agent(base_url: str, headers: dict, stop: threading.Event, handled: list) -> None:
    """Claim queued read_logs operations like the node agent does: the first returns lines, later ones none."""
    with httpx.Client(base_url=base_url, headers=headers, timeout=10) as http:
        while not stop.is_set():
            try:
                http.post(f"/v1/nodes/{NODE}/heartbeat", json={})
                claimed = http.get(f"/v1/nodes/{NODE}/operations/next").json().get("operation")
                if claimed:
                    lines = ["server started", f"password={LEAKED_VALUE}"] if not handled else []
                    handled.append(claimed["id"])
                    http.post(f"/v1/nodes/{NODE}/operations/{claimed['id']}/result",
                              json={"status": "completed", "result": {"available": True, "lines": lines}})
            except httpx.HTTPError:
                pass
            stop.wait(0.2)


def test_log_viewer_page_shows_lines_and_empty_message_after_node_completes_the_read(ctx, tmp_path):
    import uvicorn
    from fastapi.testclient import TestClient

    ctx.settings.console_session_secret = SESSION_SECRET
    app = create_app(ctx)
    app.state.sync_accounts()
    with TestClient(app) as bootstrap:
        joined = join_node(bootstrap, NODE, role="control")
        assert joined.status_code == 200
        node_headers = {"Authorization": "Bearer " + joined.json()["node_token"]}
    mount_ui(app, ctx)

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    base_url = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)

    config_path = tmp_path / "log-browser-config.json"
    config_path.write_text(json.dumps({
        "baseUrl": base_url, "cookieName": COOKIE_NAME, "secret": LEAKED_VALUE,
        "cookie": issue_session("admin@example.test", "administrator", 2_000_000_000, SESSION_SECRET),
        "firstSource": "Container · aiflow4-control-api-a"}))
    playwright_cli = shutil.which("playwright")
    node = shutil.which("node")
    assert node and playwright_cli, "Node.js and Playwright are required for the log viewer browser test"
    env = os.environ.copy()
    env.update({"AIFLOW4_LOG_BROWSER_CONFIG": str(config_path),
                "AIFLOW4_LOG_BROWSER_MODULE": str(Path(playwright_cli).resolve().parent)})

    stop, handled = threading.Event(), []
    agent = threading.Thread(target=fake_node_agent, args=(base_url, node_headers, stop, handled), daemon=True)
    try:
        server_thread.start()
        deadline = time.monotonic() + 10
        while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "log viewer test server did not start"
        agent.start()
        result = subprocess.run([node, str(SUITE)], env=env, capture_output=True, text=True, timeout=180)
        if result.returncode:
            pytest.fail("Log viewer browser suite failed.\n" + result.stderr[-8000:].replace(LEAKED_VALUE, "[REDACTED]"))
        assert "JAIF-175 log viewer browser suite passed" in result.stdout
        assert len(handled) == 2
    finally:
        stop.set()
        agent.join(timeout=5)
        server.should_exit = True
        if server_thread.is_alive():
            server_thread.join(timeout=5)
        server_socket.close()
