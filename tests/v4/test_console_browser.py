"""Console browser journeys (JAIF-252): real v4 API, fixture PostgreSQL/Redis and the installed Chromium browser.

The install provisions the first Administrator; this suite starts from that documented first-run state and drives
the first browser sign-in, role menus, screens and common actions. Browser interaction stays in the companion Node
Playwright runner so it can use the machine's installed Playwright/Chromium toolchain.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from aiflow.v4.control import login
from aiflow.v4.control import api as control_api
from aiflow.v4.control.api import create_app
from aiflow.v4.control.console_auth import CANONICAL_ROLES
from aiflow.v4.control.db import J
from aiflow.v4.control.ui import mount_ui
from conftest import add_ticket, join_node

ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).with_name("console_browser_suite.cjs")
ROLE_LABELS = {
    "administrator": "Administrator",
    "product_owner": "Product owner",
    "delivery": "Delivery",
    "team_lead": "Team Lead",
    "sa": "SA",
    "ba": "BA",
    "business": "Business",
    "reviewer": "Reviewer",
    "stakeholder": "Stakeholder",
}
VIEW_LABELS = {
    "dashboard": "Dashboard",
    "work_board": "Work board",
    "delivery": "Delivery",
    "sa": "SA",
    "team_lead": "Team Lead",
    "business": "Business",
    "human_review": "Human review",
    "stakeholder": "Stakeholder",
    "machines_ai": "Machines and AI",
    "users": "Users and roles",
    "knowledge": "Knowledge",
    "integrations": "Integrations",
    "projects": "Projects",
    "system_check": "System check",
    "licensing": "Licence",
    "backup": "Backup & Restore",
    "notifications": "Notifications",
    "settings": "Settings",
    "security": "Security",
    "support": "Support",
}


# Written out by hand on purpose: it is the expected policy, so it must not be derived from the production
# permission tables the console is being tested against.
_EVERYONE = {"Dashboard", "Work board", "Security"}
EXPECTED_ROLE_VIEWS = {
    "administrator": set(VIEW_LABELS.values()),
    "product_owner": _EVERYONE | {"Delivery", "Business", "Team Lead", "Stakeholder", "Human review", "Knowledge"},
    "delivery": _EVERYONE | {"Delivery", "Human review"},
    "team_lead": _EVERYONE | {"Delivery", "Team Lead", "Human review", "Knowledge", "System check"},
    "sa": _EVERYONE | {"SA", "Machines and AI", "Integrations", "Knowledge", "System check"},
    "ba": _EVERYONE | {"Business", "Knowledge"},
    "business": _EVERYONE | {"Business"},
    "reviewer": _EVERYONE | {"Human review"},
    "stakeholder": _EVERYONE | {"Stakeholder"},
}


def _seed_review_ticket(ctx, key: str, pr_number: int) -> None:
    add_ticket(ctx.db, key, status="Human review", labels=("require-human",))
    ctx.db.run("UPDATE tickets SET type='Task' WHERE key=%s", (key,))
    for step, outcome, pr_url, summary, details in (
        ("dev", "done", f"https://github.com/jooservices/test-aiflow-wp/pull/{pr_number}",
         "Browser review sample", {}),
        ("verify", "human", "", "Browser verify passed", {"verdict": "PASS", "checks": "fixture checks passed"}),
    ):
        ctx.db.run(
            "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,pr_url,summary,details)"
            " VALUES (%s,%s,'TESTWP',%s,'codex-01','codex','node1',1,'finished',%s,%s,%s,%s)",
            (str(uuid.uuid4()), key, step, outcome, pr_url, summary, J(details)),
        )


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in {"headers", "cookies"} and isinstance(item, list):
                sanitized[key] = []
                for entry in item:
                    name = str(entry.get("name", "")).casefold() if isinstance(entry, dict) else ""
                    if isinstance(entry, dict) and (name in {"authorization", "cookie", "set-cookie", "x-api-key"}
                                                    or any(part in name for part in ("token", "secret", "password", "session"))):
                        sanitized[key].append({**_redact(entry, secrets), "value": "[REDACTED]"})
                    else:
                        sanitized[key].append(_redact(entry, secrets))
                continue
            sensitive = (normalized in {"authorization", "cookie", "set_cookie", "token", "secret", "command",
                                        "sign_in_url", "signin_url", "private_key", "pem",
                                        "postdata", "post_data", "requestbody", "request_body"}
                         or any(part in normalized for part in ("password", "_token", "_secret", "_cookie")))
            if sensitive:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _redact(item, secrets)
        return sanitized
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(aiflow4_session=)[^;\s\"']+", r"\1[REDACTED]", value)
        return value
    return value


def _sanitize_traces(directory: Path, secrets: tuple[str, ...]) -> None:
    """Keep Playwright's action trace, but remove credentials, cookies, payload bodies and binary resources."""
    for trace in directory.glob("*.zip"):
        temp_name = ""
        try:
            with zipfile.ZipFile(trace, "r") as source:
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as temp:
                    temp_name = temp.name
                with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as output:
                    for item in source.infolist():
                        if item.filename.startswith("resources/"):
                            continue
                        raw = source.read(item.filename)
                        if item.filename.endswith((".trace", ".json", ".jsonl", ".network")):
                            lines = []
                            for line in raw.decode("utf-8", "replace").splitlines():
                                try:
                                    event = json.loads(line)
                                    if event.get("type") == "frame-snapshot":
                                        continue
                                    line = json.dumps(_redact(event, secrets), separators=(",", ":"))
                                except (json.JSONDecodeError, TypeError):
                                    line = _redact(line, secrets)
                                lines.append(line)
                            raw = ("\n".join(lines) + "\n").encode()
                        else:
                            raw = _redact(raw.decode("utf-8", "replace"), secrets).encode()
                        output.writestr(item, raw)
            os.replace(temp_name, trace)
        except Exception:
            # An interrupted browser can leave a partial archive; never attach it without sanitizing it.
            trace.unlink(missing_ok=True)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)


def test_playwright_trace_sanitizer_keeps_actions_and_removes_credentials_and_page_snapshots(tmp_path):
    trace = tmp_path / "failed.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("trace.trace", "\n".join((
            json.dumps({"type": "before", "method": "click", "params": {"selector": "Submit"}}),
            json.dumps({"type": "frame-snapshot", "snapshot": "AIFLOW_NODE_TOKEN=sample-secret"}),
            json.dumps({"type": "event", "headers": [{"name": "Authorization", "value": "Bearer sample-secret"}],
                        "cookies": [{"name": "aiflow4_session", "value": "session-secret"}],
                        "initial_password": "sample-password", "command": "AIFLOW_NODE_TOKEN=sample-secret"}),
        )))
        archive.writestr("trace.network", json.dumps({"snapshot": {
            "request": {"headers": [{"name": "Cookie", "value": "aiflow4_session=session-secret"}]},
            "response": {"content": {"text": "sample-password"}},
        }}))
        archive.writestr("resources/response", "AIFLOW_NODE_TOKEN=sample-secret")

    _sanitize_traces(tmp_path, ("sample-secret", "sample-password", "session-secret"))
    with zipfile.ZipFile(trace) as archive:
        assert "resources/response" not in archive.namelist()
        content = archive.read("trace.trace").decode()
        network = archive.read("trace.network").decode()
        assert '"method":"click"' in content
        assert "frame-snapshot" not in content
        assert "sample-secret" not in content and "sample-password" not in content and "session-secret" not in content
        assert "sample-secret" not in network and "sample-password" not in network and "session-secret" not in network

    broken_trace = tmp_path / "interrupted.zip"
    broken_trace.write_bytes(b"partial Playwright trace")
    _sanitize_traces(tmp_path, ("sample-secret",))
    assert not broken_trace.exists()


def test_console_journeys_for_roles_screens_themes_and_phone(ctx, tmp_path, monkeypatch):
    """Cover first Administrator sign-in, each role's menu, all screens and the ticket's main user journeys."""
    import uvicorn

    ctx.settings.console_login = True
    # Each browser persona and color/viewport mode receives a simulated source IP so the real per-IP
    # rate limiter does not combine independent users behind TestClient's one loopback address.
    ctx.settings.trusted_proxies = ["127.0.0.1/32"]
    ctx.settings.console_session_secret = "jaif-252-browser-session-secret-for-test-only"
    ctx.settings.reviewer_token = "jaif-252-reviewer-token-for-test-only"
    ctx.settings.public_url = ""

    user_cases = []
    credentials = []
    for role in CANONICAL_ROLES:
        email = f"jaif252-{role.replace('_', '-')}@example.test"
        # The installer seeds the first owner as `super_admin`; it is persisted as the Administrator role.
        password = login.seed_user(ctx.db, email, "super_admin" if role == "administrator" else role,
                                   ROLE_LABELS[role])
        assert password
        credentials.append(password)
        allowed = [label for label in VIEW_LABELS.values() if label in EXPECTED_ROLE_VIEWS[role]]
        user_cases.append({"role": role, "label": ROLE_LABELS[role], "email": email, "password": password,
                           "views": allowed})

    add_ticket(ctx.db, "TESTWP-980", status="To Do", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Bug' WHERE key='TESTWP-980'")
    add_ticket(ctx.db, "TESTWP-970", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic', summary='Browser test roadmap' WHERE key='TESTWP-970'")
    add_ticket(ctx.db, "TESTWP-981", status="To Do", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Story', parent_key='TESTWP-970', summary='Searchable browser sample'"
               " WHERE key='TESTWP-981'")
    add_ticket(ctx.db, "TESTWP-982", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Task', summary='Not ready sample' WHERE key='TESTWP-982'")
    add_ticket(ctx.db, "TESTWP-983", status="To Do", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Story', summary='Active run inbox browser sample' WHERE key='TESTWP-983'")
    action_keys = {}
    pr_data = {}
    next_pr = 25200
    for mode in ("light", "dark", "phone"):
        action_keys[mode] = {}
        for action in ("approve", "reject", "rework", "ask-ai"):
            key = f"TESTWP-{next_pr}"
            _seed_review_ticket(ctx, key, next_pr)
            action_keys[mode][action] = key
            pr_data[next_pr] = {"number": next_pr, "html_url": f"https://github.com/jooservices/test-aiflow-wp/pull/{next_pr}",
                                "title": f"{key}: Browser sample", "additions": 3, "deletions": 1, "changed_files": 1}
            next_pr += 1
    ctx.github.pr_data = pr_data

    admin = user_cases[0]
    config = {"baseUrl": "", "roles": user_cases, "adminEmail": admin["email"],
              "adminPassword": admin["password"], "reviewerToken": ctx.settings.reviewer_token,
              "actionKeys": action_keys,
              "project": "TESTWP", "browserCommandKey": "TESTWP-981"}

    # The add-machine UI needs a current signed release to compose its one-time join command.
    # Release verification has separate install acceptance coverage; these tiny fixture assets are never installed.
    release_root = tmp_path / "releases"
    release_version = "v4.1.0"
    release_dir = release_root / release_version
    release_dir.mkdir(parents=True)
    tarball = release_dir / f"aiflow4-{release_version}.tar.gz"
    tarball.write_bytes(b"browser-test-release-fixture")
    (release_dir / f"{tarball.name}.sigstore.json").write_text("{}")
    (release_dir / "SHA256SUMS").write_text(f"{hashlib.sha256(tarball.read_bytes()).hexdigest()}  {tarball.name}\n")
    (release_root / "CURRENT").write_text(f"{release_version}\n")
    monkeypatch.setattr(control_api, "RELEASES", release_root)

    app = create_app(ctx)
    app.state.sync_accounts()
    from fastapi.testclient import TestClient

    with TestClient(app) as bootstrap:
        joined = join_node(bootstrap, "jaif252-existing-machine", ip="127.0.0.2")
        assert joined.status_code == 200
        heartbeat = bootstrap.post("/v1/nodes/jaif252-existing-machine/heartbeat", json={"slots_total": 2},
                                   headers={"Authorization": f"Bearer {joined.json()['node_token']}"})
        assert heartbeat.status_code == 200
        inbox_node = join_node(bootstrap, "cc", ip="127.0.0.3")
        # The run stays live while the three browser modes execute before its checkpoint lifecycle is driven.
        ctx.settings.lease_ms = max(ctx.settings.lease_ms, 900_000)
        inbox_claim = bootstrap.post("/v1/work/claim", json={"ticket": "TESTWP-983", "account": "claude-01"},
                                     headers={"Authorization": f"Bearer {inbox_node.json()['node_token']}"})
        assert inbox_claim.status_code == 200 and inbox_claim.json()["claimed"]
        config["inboxRunId"] = inbox_claim.json()["run_id"]
        config["inboxNodeToken"] = inbox_node.json()["node_token"]
        config["inboxFence"] = inbox_claim.json()["fence"]
    mount_ui(app, ctx)
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    config["baseUrl"] = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
    trace_dir = Path(os.environ.get("AIFLOW4_PLAYWRIGHT_TRACE_DIR", str(tmp_path / "playwright-traces"))).resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "console-browser-config.json"
    config_path.write_text(json.dumps(config))
    playwright_cli = shutil.which("playwright")
    node = shutil.which("node")
    assert node, "Node.js is required for the console browser suite"
    assert playwright_cli, "Playwright is required for the console browser suite"
    module_path = str(Path(playwright_cli).resolve().parent)
    env = os.environ.copy()
    env.update({"AIFLOW4_CONSOLE_BROWSER_CONFIG": str(config_path),
                "AIFLOW4_CONSOLE_BROWSER_MODULE": module_path,
                "AIFLOW4_PLAYWRIGHT_TRACE_DIR": str(trace_dir)})
    started_at = time.monotonic()
    try:
        server_thread.start()
        deadline = time.monotonic() + 10
        while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Console browser test server did not start"
        try:
            result = subprocess.run([node, str(SUITE)], env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired as exc:
            _sanitize_traces(trace_dir, tuple(credentials) + (ctx.settings.console_session_secret,
                                                               ctx.settings.reviewer_token))
            pytest.fail(f"Console browser suite exceeded the 10-minute acceptance limit. {exc}")
        _sanitize_traces(trace_dir, tuple(credentials) + (ctx.settings.console_session_secret,
                                                           ctx.settings.reviewer_token))
        elapsed = time.monotonic() - started_at
        safe_stderr = result.stderr
        for secret in credentials + [ctx.settings.console_session_secret, ctx.settings.reviewer_token]:
            safe_stderr = safe_stderr.replace(secret, "[REDACTED]")
        if result.returncode:
            pytest.fail(f"Console browser suite failed after {elapsed:.1f}s.\n{safe_stderr[-12000:]}")
        assert elapsed < 600, f"Console browser suite took {elapsed:.1f}s; AC5 requires under 600s"
        assert "JAIF-252 browser suite passed" in result.stdout
    finally:
        server.should_exit = True
        if server_thread.is_alive():
            server_thread.join(timeout=5)
        server_socket.close()
