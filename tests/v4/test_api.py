import csv
import io
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import add_open_escalation, add_ticket, join_node, report_account_credentials
from aiflow.v4.control import api as control_api
from aiflow.v4.control.db import J


def join(client, name, ip="", *, heartbeat=True):
    r = join_node(client, name, ip=ip, heartbeat=heartbeat)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def test_join_requires_the_join_token(client):
    assert client.post("/v1/nodes/join-tokens", json={"provision_key": "nope", "name": "cc",
                                                       "project": "TESTWP"}).status_code == 403
    assert client.post("/v1/nodes/join", json={"join_token": "nope"}).status_code == 403
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}).status_code == 401


def test_join_tokens_bind_node_role_and_project_and_can_only_be_redeemed_once(client, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    (tmp_path / "CURRENT").write_text("v1.2.3")
    issued = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "node-new",
                                                         "role": "worker", "project": "TESTWP",
                                                         "ai_account": "must-not-be-stored"})
    assert issued.status_code == 200
    data = issued.json()
    assert set(data) == {"token", "name", "role", "project", "expires_at"}
    assert data["name"] == "node-new" and data["role"] == "worker" and data["project"] == "TESTWP"
    assert len(data["token"]) >= 40 and "must-not-be-stored" not in data["token"]
    expires_at = datetime.fromisoformat(data["expires_at"])
    assert timedelta(0) < expires_at - datetime.now(timezone.utc) <= timedelta(minutes=5)
    preview = client.post("/v1/nodes/join-tokens/inspect", json={"join_token": data["token"]})
    assert preview.status_code == 200
    assert preview.json() == {"name": "node-new", "role": "worker", "project": "TESTWP", "stack": "",
                              "stacks": [], "release": "v1.2.3"}

    joined = client.post("/v1/nodes/join", json={"join_token": data["token"]})
    assert joined.status_code == 200
    assert {k: joined.json()[k] for k in ("node", "role", "project", "stack", "release")} == {
        "node": "node-new", "role": "worker", "project": "TESTWP", "stack": "", "release": "v1.2.3"}
    node = ctx.db.one("SELECT role,projects,token_hash FROM nodes WHERE name='node-new'")
    assert node["role"] == "worker" and node["projects"] == ["TESTWP"]
    assert node["token_hash"] != joined.json()["node_token"]
    assert client.post("/v1/nodes/join", json={"join_token": data["token"]}).status_code == 403
    assert client.post("/v1/nodes/join-tokens/inspect", json={"join_token": data["token"]}).status_code == 403

    expired = client.post("/v1/nodes/join-tokens", json={"provision_key": "join-secret", "name": "node-expired",
                                                         "role": "worker", "project": "TESTWP"}).json()["token"]
    ctx.db.run("UPDATE node_join_tokens SET expires_at=now()-interval '1 second' WHERE token_hash=%s",
               (control_api.h(expired),))
    assert client.post("/v1/nodes/join", json={"join_token": expired}).status_code == 403
    assert client.post("/v1/nodes/join-tokens/inspect", json={"join_token": expired}).status_code == 403


def test_developer_pr_lookup_is_fenced_and_scoped_to_the_run_repository(client, ctx):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-6")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-6", "account": "claude-01"},
                          headers=cc).json()
    url = "https://github.com/jooservices/test-aiflow-wp/pull/42"
    ctx.github.prs.append({"repo": "test-aiflow-wp", "head": {"ref": "TESTWP-6-no-change", "sha": "abc123"},
                           "state": "open", "number": 42, "html_url": url})

    found = client.get("/v1/github/pr", params={"run_id": claimed["run_id"], "fence": claimed["fence"],
                                                "repo": "jooservices/test-aiflow-wp",
                                                "head": "TESTWP-6-no-change"}, headers=cc)
    assert found.status_code == 200 and found.json() == {"found": True, "url": url, "number": 42,
                                                         "head_sha": "abc123"}
    missing = client.get("/v1/github/pr", params={"run_id": claimed["run_id"], "fence": claimed["fence"],
                                                  "repo": "jooservices/test-aiflow-wp",
                                                  "head": "TESTWP-6-other"}, headers=cc)
    assert missing.status_code == 200 and missing.json() == {"found": False}
    wrong_repo = client.get("/v1/github/pr", params={"run_id": claimed["run_id"], "fence": claimed["fence"],
                                                     "repo": "jooservices/other", "head": "TESTWP-6-no-change"},
                            headers=cc)
    assert wrong_repo.status_code == 403


def test_worker_poll_counts_running_project_tickets_only_when_a_limit_is_configured(client, ctx):
    node = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-PARALLEL-LIMIT")
    original_all = ctx.db.all
    project_count_queries = []

    def recording_all(sql, params=()):
        if "SELECT project,count(*) AS count FROM runs" in sql:
            project_count_queries.append((sql, params))
        return original_all(sql, params)

    ctx.db.all = recording_all
    response = client.get("/v1/work/candidates?account=claude-01", headers=node)
    assert response.status_code == 200
    assert "TESTWP-PARALLEL-LIMIT" in {ticket["key"] for ticket in response.json()["tickets"]}
    assert project_count_queries == []  # the fixture project's limit is zero (unlimited)

    ctx.repo().projects["TESTWP"].limits["parallel"] = 1
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status)"
               " VALUES (%s,'TESTWP-ACTIVE','TESTWP','dev','codex-01','codex','node1',1,'running')",
               (str(uuid.uuid4()),))
    response = client.get("/v1/work/candidates?account=claude-01", headers=node)
    assert response.status_code == 200
    assert response.json()["tickets"] == []
    assert len(project_count_queries) == 1
    assert "project=ANY(%s)" in project_count_queries[0][0]


def test_board_snapshot_marks_already_done_runs_and_keeps_the_pull_request_link(client, ctx):
    add_ticket(ctx.db, "TESTWP-8", status="In Verify")
    url = "https://github.com/jooservices/test-aiflow-wp/pull/43"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,details,pr_url,started_at,ended_at)"
               " VALUES (%s,'TESTWP-8','TESTWP','dev','claude-01','claude','cc',1,'finished','done',%s,%s,%s,now()-interval '1 minute',now())",
               (str(uuid.uuid4()), "Already done: no new changes were needed", J({"already_done": True, "no_changes": True}), url))

    ticket = next(row for row in client.get("/v1/state").json()["tickets"] if row["key"] == "TESTWP-8")

    assert ticket["last_run"]["already_done"] is True
    assert ticket["already_done"]["pr_url"] == url


def test_already_done_marker_survives_a_later_verifier_run_without_verdict(client, ctx):
    add_ticket(ctx.db, "TESTWP-10", status="In Verify")
    url = "https://github.com/jooservices/test-aiflow-wp/pull/44"
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,details,pr_url,started_at,ended_at)"
               " VALUES (%s,'TESTWP-10','TESTWP','dev','claude-01','claude','cc',1,'finished','done','Already done',%s,%s,now()-interval '5 minute',now()-interval '4 minute')",
               (str(uuid.uuid4()), J({"already_done": True}), url))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,summary,details,started_at,ended_at)"
               " VALUES (%s,'TESTWP-10','TESTWP','verify','codex-01','codex','cc',2,'finished','quota','no verdict',%s,now()-interval '2 minute',now())",
               (str(uuid.uuid4()), J({})))

    ticket = next(row for row in client.get("/v1/state").json()["tickets"] if row["key"] == "TESTWP-10")

    assert ticket["last_run"]["step"] == "verify" and ticket["last_run"]["already_done"] is False
    assert ticket["already_done"]["pr_url"] == url
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-10'")
    ticket = next(row for row in client.get("/v1/state").json()["tickets"] if row["key"] == "TESTWP-10")
    assert ticket["already_done"] is None


def test_bootstrap_installer_uses_pinned_server_installer_and_signed_ai_flow_release(client):
    response = client.get("/v1/install.sh")
    assert response.status_code == 200
    script = response.text
    assert "CONTROL_URL=http://testserver" in script
    assert 'python3 - "$CONTROL_URL" "$NODE_NAME" "$NODE_TOKEN_FILE" > /etc/aiflow/node.env' in script
    assert "SERVER_INSTALLER_TAG=v1.1.0" in script
    assert "apply --modules packages,timesync,docker,docker_group --preflight" in script
    assert "SI_PHP_VERSION" in script and "--modules php --preflight" in script
    assert "useradd --system" in script and "aiflow-worker" in script
    assert "npm install --global @anthropic-ai/claude-code @openai/codex" in script
    assert "node_token" in script
    assert "__AIFLOW4_CONTROL_URL__" not in script

    syntax = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    # Account for each external command invoked by the bootstrap and assert where it comes from.
    package_block = script.split('BOOTSTRAP_PACKAGES="', 1)[1].split('"', 1)[0]
    packages = set(package_block.split())
    package_sources = {
        "bash": "bash", "curl": "curl", "python3": "python3", "tar": "tar", "chmod": "coreutils", "rm": "coreutils",
        "install": "coreutils", "cat": "coreutils", "ln": "coreutils", "readlink": "coreutils",
        "dirname": "coreutils", "sha256sum": "coreutils", "uname": "coreutils", "mkdir": "coreutils",
        "sed": "sed", "find": "findutils", "useradd": "passwd", "npm": "npm", "codex": "npm",
    }
    assert set(package_sources.values()) <= packages
    for executable, package in package_sources.items():
        assert executable in script
        assert package in packages
    assert all(marker in script for marker in ("#!/bin/sh", "id -u", "mktemp -d", "command -v apt-get",
                                                "command -v dnf", "apt-get install", "dnf install"))
    assert "server-installer-${SERVER_INSTALLER_TAG#v}/bin/server-installer" in script
    assert "UV_INSTALL_DIR=/usr/local/bin sh \"$WORK/uv-install.sh\"" in script
    assert "npm install --global @anthropic-ai/claude-code @openai/codex @vibe-kit/grok-cli opencode-ai" in script
    assert "SI_PHP_VERSION=$PHP_VERSION SI_PHP_MODE=cli \"$SI_BIN\" apply --modules php" in script
    assert "getcomposer.org/installer" in script and "--filename=composer" in script
    assert "wp-cli/builds/gh-pages/phar/wp-cli.phar" in script
    assert "codex-code-mode-host-${CODEX_HOST_ARCH}" in script
    assert "install -m 0755 \"$WORK/cosign\" /usr/local/bin/cosign" in script
    assert "SERVER_INSTALLER_TAG=v1.1.0" in script
    verify_at = script.index('verify-blob "$WORK/release.tar.gz"')
    extract_at = script.index('tar -xzf "$WORK/release.tar.gz" -C "$WORK/release-bootstrap"')
    install_at = script.index('"$RELEASE_INSTALLER" "$WORK/release.tar.gz"')
    assert verify_at < extract_at < install_at
    assert 'RELEASE_INSTALLER=$WORK/release-bootstrap/deploy/v4/bin/aiflow4-node-install' in script
    assert '"$RELEASE_INSTALLER" "$WORK/release.tar.gz" "$RELEASE_VERSION"' in script
    assert '/usr/local/sbin/aiflow4-node-install' not in script

    writer_marker = 'python3 - "$CONTROL_URL" "$NODE_NAME" "$NODE_TOKEN_FILE" > /etc/aiflow/node.env <<\'PY\'\n'
    writer = script.split(writer_marker, 1)[1].split("\nPY\n", 1)[0]
    env_file = subprocess.run(
        ["python3", "-c", writer, "https://control.example", "node-new", "/srv/aiflow/state/node.token"],
        capture_output=True, text=True,
    )
    assert env_file.returncode == 0, env_file.stderr
    assert env_file.stdout.splitlines() == [
        'AIFLOW4_CONTROL_URL="https://control.example"',
        'AIFLOW4_NODE_NAME="node-new"',
        'AIFLOW4_NODE_TOKEN_FILE="/srv/aiflow/state/node.token"',
        'AIFLOW4_SLOTS="2"',
        'AIFLOW4_POLICY_RELAY="1"',
        'AIFLOW4_SELF_UPDATE="1"',
    ]
    unsafe_env_file = subprocess.run(
        ["python3", "-c", writer, "https://control.example$(touch${IFS}/tmp/pwned)", "node-new",
         "/srv/aiflow/state/node.token"], capture_output=True, text=True,
    )
    assert unsafe_env_file.returncode != 0


def test_bootstrap_python_uses_shared_uv_install_without_replacing_python3(client):
    response = client.get("/v1/install.sh")
    assert response.status_code == 200
    script = response.text

    assert script.index("umask 022") < script.index("UV_PYTHON_INSTALL_DIR=/opt/aiflow/python uv python install 3.12")
    assert "install -d -m 0755 /opt/aiflow/python" in script
    assert "UV_PYTHON_INSTALL_DIR=/opt/aiflow/python uv python install 3.12" in script
    assert "PY312=$(UV_PYTHON_INSTALL_DIR=/opt/aiflow/python uv python find 3.12)" in script
    assert 'ln -sfn "$PY312" /usr/local/bin/python3.12' in script
    # A root-run bootstrap must never make generic python3 resolve through root's 0700 home.
    python_links = [line.strip() for line in script.splitlines()
                    if line.lstrip().startswith("ln ") and "/usr/local/bin/python3" in line]
    assert python_links == ['ln -sfn "$PY312" /usr/local/bin/python3.12']
    assert "/root/.local/share/uv/python" not in script


def test_install_script_rejects_shell_syntax_in_host_headers(client):
    hostile_host = "control.example$(touch${IFS}/tmp/aiflow-host-injection)"
    for headers in ({"host": hostile_host}, {"x-forwarded-host": hostile_host}):
        response = client.get("/v1/install.sh", headers=headers)
        assert response.status_code == 400


def test_install_script_accepts_dns_and_ip_hosts_with_optional_ports(client):
    dns = client.get("/v1/install.sh", headers={"host": "control.example:8443", "x-forwarded-proto": "https"})
    assert dns.status_code == 200
    assert "CONTROL_URL=https://control.example:8443" in dns.text

    ipv6 = client.get("/v1/install.sh", headers={"host": "[2001:db8::1]:8080"})
    assert ipv6.status_code == 200
    assert "CONTROL_URL='http://[2001:db8::1]:8080'" in ipv6.text


def test_sync_accounts_restores_projects_for_configured_node_rows_without_join_tokens(client, ctx):
    # Model a node row created by the old registry migration before project binding was added.
    ctx.db.run("UPDATE nodes SET projects='{}',token_hash='',dynamic=false WHERE name='node1'")
    before = ctx.db.one("SELECT token_hash,projects FROM nodes WHERE name='node1'")
    assert before == {"token_hash": "", "projects": []}

    client.app.state.sync_accounts()
    after = ctx.db.one("SELECT token_hash,projects FROM nodes WHERE name='node1'")
    assert after == {"token_hash": "", "projects": ["TESTWP"]}
    assert ctx.repo().projects_for_node("node1", before["projects"], dynamic=False) == ["TESTWP"]

    # A later control repo project assignment also takes effect without joining again.
    from aiflow.v4.core.controlrepo import Project

    ctx.repo().projects["TESTFLOW"] = Project(key="TESTFLOW", repo="jooservices/testflow")
    ctx.repo().nodes["node1"].projects = ["TESTFLOW"]
    assert ctx.repo().projects_for_node("node1", after["projects"], dynamic=False) == ["TESTFLOW"]


def test_command_center_shows_copyable_join_command_and_device_code_sign_in_guide(client):
    page = Path("aiflow/v4/control/ui/console.html").read_text()
    docs = " ".join(Path("docs/v4/node-join.md").read_text().split())
    assert "Join a node" in page and "join-command" in page and "Show one-time command" in page
    assert "--token ' + d.token" not in page and "AIFLOW4_JOIN_TOKEN" not in page
    assert "AIFLOW4_JOIN_TOKEN" not in docs and "one-use enrollment grant" in docs
    assert "sign-in link and code" in docs and "browser" in docs
    assert "provider login commands in a terminal" in docs and "codex login --device-auth" not in docs
    assert "AI provider credential" in docs and "never sent to the control plane" in docs


def test_joined_project_node_is_online_preflight_green_and_can_claim_project_ticket(client, ctx):
    headers = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-71")
    heartbeat = client.post("/v1/nodes/node1/heartbeat", json={
        "tools": {"git": "git version 2", "uv": "uv 0.1", "codex": "codex 1.0",
                  "codex-code-mode-host": "present /usr/local/bin/codex-code-mode-host"},
        "slots_total": 2,
    }, headers=headers)
    assert heartbeat.status_code == 200
    assert heartbeat.json()["desired"]["projects"] == ["TESTWP"]
    node = next(n for n in client.get("/v1/state").json()["nodes"] if n["name"] == "node1")
    assert node["status"] == "online" and node["missing_tools"] == [] and node["projects"] == ["TESTWP"]
    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=headers)
    assert candidates.status_code == 200
    assert [t["key"] for t in candidates.json()["tickets"]] == ["TESTWP-71"]


def test_node_health_reports_heartbeat_and_preflight_for_the_installer(client, ctx):
    token = client.post("/v1/nodes/join-tokens", json={
        "provision_key": "join-secret", "name": "node-health", "project": "TESTWP"})
    assert token.status_code == 200
    joined = client.post("/v1/nodes/join", json={"join_token": token.json()["token"]})
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    before = client.get("/v1/nodes/node-health/health", headers=headers).json()
    assert before["status"] != "online" and before["preflight_ok"] is False and before["missing_tools"]
    client.post("/v1/nodes/node-health/heartbeat", json={
        "tools": {"git": "git version 2", "uv": "uv 0.1", "codex": "codex 1.0",
                  "codex-code-mode-host": "present /usr/local/bin/codex-code-mode-host"},
        "slots_total": 2}, headers=headers)
    after = client.get("/v1/nodes/node-health/health", headers=headers).json()
    assert after["status"] == "online" and after["preflight_ok"] is True and after["missing_tools"] == []
    assert 0 <= after["heartbeat_age_seconds"] < 60
    assert client.get("/v1/nodes/node-health/health").status_code == 401
    other = join(client, "node2")
    assert client.get("/v1/nodes/node-health/health", headers=other).status_code == 403


def test_node_heartbeat_is_shipped_to_openobserve_only(client, ctx):
    ctx.events.shipper.url = "http://o2.test"  # queue only; the shipping thread is not started
    cc = join(client, "cc")
    ctx.events.shipper.q.queue.clear()  # discard the test helper's simulated first agent heartbeat
    response = client.post("/v1/nodes/cc/heartbeat", json={
        "slots_total": 4, "slots_used": 2, "release_version": "4.0.0", "metrics": {"cpu_pct": 12.5}
    }, headers=cc)

    assert response.status_code == 200
    shipped = [e for e in list(ctx.events.shipper.q.queue) if e["type"] == "node.heartbeat"]
    assert len(shipped) == 1
    assert shipped[0]["node"] == "cc" and shipped[0]["outcome"] == "online"
    assert shipped[0]["slots_total"] == 4 and shipped[0]["metrics"] == {"cpu_pct": 12.5}
    assert isinstance(shipped[0]["_timestamp"], int)
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='node.heartbeat'")["n"] == 0


def test_node_heartbeat_persists_only_its_accounts_credential_expiry(client, ctx):
    cc = join(client, "cc")
    expires_at = datetime.now(timezone.utc) + timedelta(days=3)
    response = client.post("/v1/nodes/cc/heartbeat", json={
        "credential_expiries": {"claude-01": expires_at.isoformat(), "codex-01": expires_at.isoformat()}
    }, headers=cc)
    assert response.status_code == 200
    assert ctx.db.one("SELECT credential_expires_at FROM accounts WHERE id='claude-01'")["credential_expires_at"] == expires_at
    assert ctx.db.one("SELECT credential_expires_at FROM accounts WHERE id='codex-01'")["credential_expires_at"] is None

    client.post("/v1/nodes/cc/heartbeat", json={"credential_expiries": {"claude-01": None}}, headers=cc)
    assert ctx.db.one("SELECT credential_expires_at FROM accounts WHERE id='claude-01'")["credential_expires_at"] is None


def test_sa_state_tracks_capacity_recommendations_and_control_repo_thresholds(client, ctx):
    cc = join(client, "cc")
    node1 = join(client, "node1")
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 4, "slots_used": 4}, headers=cc)
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 4, "slots_used": 0}, headers=node1)
    ctx.db.run("UPDATE sa_health SET slot_pressure_since=now() - interval '31 minutes' WHERE id=true")
    ctx.db.run("UPDATE nodes SET idle_since=now() - interval '8 days' WHERE name='node1'")
    ctx.db.run("UPDATE accounts SET status='cooldown', cooldown_since=now() - interval '16 minutes'")
    ctx.db.run("UPDATE accounts SET status='idle', cooldown_since=NULL, idle_since=now() - interval '8 days'"
               " WHERE id='opencode-01'")

    state = client.get("/v1/state").json()
    assert state["sa"]["thresholds"] == {"slots_used_percent": 80, "slots_duration_minutes": 30,
                                         "role_cooldown_minutes": 15, "idle_days": 7,
                                         "credential_expiry_days": 7}
    assert {k: state["sa"]["slots"][k] for k in ("used", "total", "utilization_percent")} == {
        "used": 4, "total": 8, "utilization_percent": 50.0}
    assert state["sa"]["slots"]["pressure_since"]
    suggestions = {(r["kind"], r["subject"]) for r in state["sa"]["recommendations"]}
    assert ("add_node", "fleet") not in suggestions  # fleet total is below the 80% threshold
    assert ("add_account", "verify") in suggestions and ("add_account", "master") in suggestions
    assert ("remove_node", "node1") in suggestions and ("remove_account", "opencode-01") in suggestions

    ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='node1'")  # 4 / 5 slots = the default 80% threshold
    state = client.get("/v1/state").json()
    assert state["sa"]["slots"]["utilization_percent"] == 80.0
    assert ("add_node", "fleet") in {(r["kind"], r["subject"]) for r in state["sa"]["recommendations"]}

    ctx.repo().sa = {"thresholds": {"slots_used_percent": 90, "slots_duration_minutes": 30,
                                    "role_cooldown_minutes": 15, "idle_days": 7}}
    state = client.get("/v1/state").json()
    assert state["sa"]["thresholds"]["slots_used_percent"] == 90
    assert ("add_node", "fleet") not in {(r["kind"], r["subject"]) for r in state["sa"]["recommendations"]}


def test_sa_surfaces_account_probe_failures_and_credential_expiry(client, ctx):
    cc = join(client, "cc")
    expiry = datetime.now(timezone.utc) + timedelta(days=5)
    assert client.post("/v1/accounts/claude-01/probe", json={
        "ok": False, "error_kind": "auth", "summary": "sign in required",
        "credential_expires_at": expiry.isoformat()}, headers=cc).status_code == 200
    assert client.post("/v1/accounts/opencode-01/probe", json={
        "ok": False, "error_kind": "provider", "summary": "provider check failed"}, headers=cc).status_code == 200
    client.post("/v1/nodes/cc/heartbeat", json={"workers": [
        {"account": "claude-01", "state": "unavailable", "probe": {"ok": False, "error_kind": "auth"}},
        {"account": "opencode-01", "state": "unavailable", "probe": {"ok": False, "error_kind": "provider"}},
    ]}, headers=cc)

    state = client.get("/v1/state").json()
    accounts = {a["id"]: a for a in state["accounts"]}
    assert accounts["claude-01"]["status"] == "signed_out"
    assert accounts["claude-01"]["credential_expiry_warning"] is True
    assert accounts["claude-01"]["credential_expires_at"].startswith(expiry.date().isoformat())
    assert accounts["opencode-01"]["status"] == "error" and accounts["opencode-01"]["check_failed"] is True
    assert accounts["codex-01"]["credential_expires_at"] == "unknown"
    assert {a["id"] for a in state["sa"]["provider_blocks"]} == {"claude-01"}


def test_sa_reports_both_policy_and_provider_blocks(client, ctx):
    ctx.events.emit("policy.decision", ticket="TESTWP-50", account="claude-01", node="cc",
                    run_id="run-50", outcome="deny", reason="policy refused command",
                    command="git push origin main")
    ctx.db.run("UPDATE accounts SET status='cooldown', cooldown_since=now() WHERE id='codex-01'")
    sa = client.get("/v1/state").json()["sa"]
    assert sa["policy_denials"][0]["ticket"] == "TESTWP-50"
    assert sa["policy_denials"][0]["account"] == "claude-01"
    assert sa["policy_denials"][0]["command"] == "git push origin main"
    assert "codex-01" in {a["id"] for a in sa["provider_blocks"]}


def test_sa_network_diagnostics_include_heartbeat_events_runs_and_retry(client, ctx):
    import uuid

    ctx.events.emit("node.heartbeat.error", node="cc", reason="node-agent could not reach control",
                    error_kind="network", retry=True, attempts=2)
    # two network failures of the same ticket and step, then a successful retry; no retry metadata is written
    for minutes, error_kind, outcome in ((30, "network", "failed"), (20, "network", "failed"), (10, "", "done")):
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,error_kind,"
                   "details,started_at) VALUES (%s,'TESTWP-51','TESTWP','dev','claude-01','cc',1,'finished',%s,%s,"
                   "%s,now() - make_interval(mins => %s))",
                   (uuid.uuid4(), outcome, error_kind, J({"error_message": "connection reset"}), minutes))
    # a single network failure of another ticket that has not been retried yet
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,error_kind)"
               " VALUES (%s,'TESTWP-52','TESTWP','dev','claude-01','cc',1,'finished','failed','network')",
               (uuid.uuid4(),))
    sa = client.get("/v1/state").json()["sa"]
    sources = {issue["source"] for issue in sa["network_issues"]}
    assert sources == {"event", "run"}
    event = next(issue for issue in sa["network_issues"] if issue["source"] == "event")
    assert event["data"]["error_kind"] == "network" and event["data"]["retry"] is True
    runs = [issue for issue in sa["network_issues"] if issue["source"] == "run"]
    retried = sorted((r for r in runs if r["ticket"] == "TESTWP-51"), key=lambda r: r["attempts"])
    assert [(r["attempts"], r["retry"]) for r in retried] == [(1, True), (2, True)]
    assert all(r["error_kind"] == "network" and r["detail"] == "connection reset" for r in retried)
    pending = next(r for r in runs if r["ticket"] == "TESTWP-52")
    assert pending["attempts"] == 1 and pending["retry"] is False


def test_command_center_events_survive_many_heartbeats(client, ctx):
    cc = join(client, "cc")
    ctx.events.emit("policy.denied", ticket="TESTWP-1", account="claude-01", reason="force push")
    for _ in range(80):
        assert client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=cc).status_code == 200
    # a stray heartbeat row (e.g. from older data) is still kept out of the list
    ctx.events.emit("node.heartbeat", node="cc")

    types = [e["type"] for e in client.get("/v1/state").json()["events"]]
    assert "node.heartbeat" not in types
    assert "policy.denied" in types and "node.joined" in types


def test_full_claim_flow_with_fencing(client, ctx, monkeypatch):
    cc = join(client, "cc")
    assert client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "workers": [{"account": "claude-01", "state": "idle"}]},
                       headers=cc).json()["desired"]["accounts"] == ["claude-01", "opencode-01"]
    add_ticket(ctx.db, "TESTWP-1", priority="High")
    add_ticket(ctx.db, "TESTWP-2")
    add_ticket(ctx.db, "TESTWP-3", labels=("aiflow-ready", "aiflow-hold"))
    add_ticket(ctx.db, "TESTWP-4", labels=())
    add_ticket(ctx.db, "TESTWP-5", blocked_by=["TESTWP-1"])
    keys = [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]]
    assert keys == ["TESTWP-1", "TESTWP-2"]
    # an account of another node is refused
    assert client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=cc).status_code == 403
    # explicit-only account gets nothing without its label
    assert client.get("/v1/work/candidates", params={"account": "opencode-01"}, headers=cc).json()["tickets"] == []

    with monkeypatch.context() as patch:
        patch.setattr(ctx.leases, "held_keys", lambda: (_ for _ in ()).throw(
            AssertionError("a scoped claim should not scan every Redis lease")))
        r = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["fence"] == 1 and r["model"] == "opus" and r["provider"] == "claude"
    assert r["project"]["repo"] == "jooservices/test-aiflow-wp" and r["identity"][0].endswith("[bot]")
    run_id, fence = r["run_id"], r["fence"]
    # second claim of the same ticket is refused (held), candidate list no longer shows it
    with monkeypatch.context() as patch:
        patch.setattr(ctx.leases, "held_keys", lambda: (_ for _ in ()).throw(
            AssertionError("an already-held claim should not scan every Redis lease")))
        duplicate = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc)
        assert duplicate.status_code == 409 and duplicate.json()["reason"] == "ticket is already claimed"
    assert [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]] == ["TESTWP-2"]

    assert client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": fence, "progress": {"phase": "ai"}}, headers=cc).status_code == 200
    assert client.post("/v1/jira/TESTWP-1/transition", json={"run_id": run_id, "fence": fence, "to": "In Progress"},
                       headers=cc).json()["status"] == "In Progress"
    github_token = client.post("/v1/github/token", json={"run_id": run_id, "fence": fence,
                                                          "repo": "jooservices/test-aiflow-wp"},
                               headers=cc).json()
    assert github_token["token"] == "ghs_fake"
    token_audit = ctx.db.one("SELECT result, payload FROM write_audit WHERE target='github' AND action='token'"
                             " AND ticket='TESTWP-1' ORDER BY id DESC LIMIT 1")
    assert token_audit["result"] == "issued" and "ghs_fake" not in str(token_audit["payload"])
    assert client.post("/v1/github/token", json={"run_id": run_id, "fence": fence, "repo": "jooservices/other"},
                       headers=cc).status_code == 403
    pr = client.post("/v1/github/pr", json={"run_id": run_id, "fence": fence, "repo": "test-aiflow-wp", "head": "TESTWP-1-x",
                                            "base": "develop", "title": "TESTWP-1: x"}, headers=cc).json()
    assert pr["url"].endswith("/pull/1")

    # the lease is taken over (expiry simulated): every write with the old fence is refused and audited
    ctx.leases.r.delete(f"{ctx.leases.p}lease:TESTWP-1")
    r2 = ctx.leases.claim("TESTWP-1", "other-run", 60_000)
    assert r2.fence == 2
    assert client.post("/v1/jira/TESTWP-1/comment", json={"run_id": run_id, "fence": fence, "body": "late"},
                       headers=cc).status_code == 409
    assert client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": fence}, headers=cc).status_code == 409
    refused = ctx.db.one("SELECT count(*) AS n FROM write_audit WHERE accepted = false")["n"]
    assert refused == 1
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (run_id,))["status"] == "lost"


def test_worker_claim_reuses_the_live_repo_overlay_once_per_request(client, ctx, monkeypatch):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-13")
    original_one = ctx.db.one
    overlay_reads = []

    def count_overlay_reads(sql, params=None):
        if "EXISTS(SELECT 1 FROM nodes WHERE removed_at IS NOT NULL)" in sql:
            overlay_reads.append(sql)
        return original_one(sql, params)

    monkeypatch.setattr(ctx.db, "one", count_overlay_reads)
    response = client.post("/v1/work/claim", json={"ticket": "TESTWP-13", "account": "claude-01"}, headers=cc)

    assert response.status_code == 200 and response.json()["claimed"] is True
    assert len(overlay_reads) == 1


def test_quota_result_cools_the_account_down(client, ctx):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-7")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-7", "account": "claude-01"}, headers=cc).json()
    res = client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "quota", "error_kind": "quota",
                                                              "quota": {"windows": {"five_hour": {"utilization": 1.0}}}},
                      headers=cc).json()
    assert res["still_owner"]
    assert client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc).json()["released"]
    assert ctx.leases.cooling("claude-01")[0] == "quota"
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"] == []
    acc = ctx.db.one("SELECT status, quota FROM accounts WHERE id='claude-01'")
    assert acc["status"] == "cooldown" and acc["quota"]["windows"]["five_hour"]["utilization"] == 1.0


def test_policy_hook_uses_the_run_token(client, ctx):
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-8")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-8", "account": "claude-01"}, headers=cc).json()
    hook = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": "/srv/aiflow/work/x"}
    out = client.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {r['run_token']}"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook["tool_input"]["command"] = "composer install"
    out = client.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {r['run_token']}"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    out = client.post("/v1/policy/decide", json=hook, headers={"Authorization": "Bearer wrong"}).json()
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_state_snapshot_and_watchdog(client, ctx):
    from aiflow.v4.control.loops import watchdog
    cc = join(client, "cc")
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "metrics": {
        "cpu_pct": 10, "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20,
        "disk_total": 100 * 1024 ** 3, "disk_free": 20 * 1024 ** 3}}, headers=cc)
    add_ticket(ctx.db, "TESTWP-9")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=cc).json()
    ctx.jira.status_of["TESTWP-9"] = "In Progress"
    s = client.get("/v1/state").json()
    assert any(n["name"] == "cc" and n["status"] == "online" for n in s["nodes"])
    assert s["active_runs"][0]["ticket"] == "TESTWP-9"
    assert next(n for n in s["nodes"] if n["name"] == "cc")["sessions_busy"] == 1
    account = next(a for a in s["accounts"] if a["id"] == "claude-01")
    assert account["runs_active"] == 1 and account["run"]["id"] == r["run_id"]
    assert any(a["id"] == "claude-01" and a["status"] == "working" for a in s["accounts"])
    # node goes silent, lease expires: watchdog marks node offline, opens an incident, requeues the ticket
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '5 minutes' WHERE name='cc'")
    ctx.leases.r.delete(f"{ctx.leases.p}lease:TESTWP-9")
    watchdog(ctx)
    assert ctx.db.one("SELECT status FROM nodes WHERE name='cc'")["status"] == "offline"
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_down' AND closed_at IS NULL")["n"] == 1
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (r["run_id"],))["status"] == "lost"
    assert ctx.jira.status_of["TESTWP-9"] == "To Do"
    # heartbeat comes back: node online, incident closed
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=cc)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_down' AND closed_at IS NULL")["n"] == 0


def test_probe_sets_account_state_and_quota_alerts(client, ctx):
    from aiflow.v4.control.loops import watchdog
    cc = join(client, "cc")
    assert client.get("/v1/accounts/claude-01/spec", headers=cc).json()["provider"] == "claude"
    assert client.get("/v1/accounts/codex-01/spec", headers=cc).status_code == 403
    q = {"windows": {"five_hour": {"utilization": 0.2, "resets_at": 1792561800},
                     "seven_day": {"utilization": 0.93, "resets_at": 1792638000}}}
    assert client.post("/v1/accounts/claude-01/probe", json={"ok": True, "quota": q, "summary": "OK",
                                                            "model": "opus", "tokens_in": 123,
                                                            "tokens_out": 45, "cost_usd": .02,
                                                            "duration_ms": 900}, headers=cc).status_code == 200
    sample = ctx.db.one("SELECT provider,model,tokens_in,tokens_out,cost_usd,duration_ms,quota"
                        " FROM account_quota_samples WHERE account_id='claude-01'")
    assert (sample["provider"], sample["model"], sample["tokens_in"], sample["tokens_out"],
            float(sample["cost_usd"]), sample["duration_ms"]) == ("claude", "opus", 123, 45, .02, 900)
    account = next(a for a in client.get("/v1/state").json()["accounts"] if a["id"] == "claude-01")
    assert account["quota_awareness"]["windows"][0]["source"] == "reported"
    assert ctx.db.one("SELECT status FROM accounts WHERE id='claude-01'")["status"] == "idle"
    watchdog(ctx)
    inc = ctx.db.one("SELECT severity, title FROM incidents WHERE kind='quota_high' AND closed_at IS NULL")
    assert inc["severity"] == "warning" and "weekly limit at 93%" in inc["title"]
    # a failed probe (balance exhausted) cools the account down and opens an incident; a good one clears both
    client.post("/v1/accounts/opencode-01/probe", json={"ok": False, "error_kind": "quota", "summary": "quota: 402"}, headers=cc)
    assert ctx.leases.cooling("opencode-01")[0] == "quota"
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='account_quota' AND subject='opencode-01'"
                      " AND closed_at IS NULL")["n"] == 1
    client.post("/v1/accounts/opencode-01/probe", json={"ok": True, "summary": "OK"}, headers=cc)
    assert ctx.leases.cooling("opencode-01") is None
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='account_quota' AND subject='opencode-01'"
                      " AND closed_at IS NULL")["n"] == 0


def test_predicted_runout_alerts_owner_and_routes_account_work_to_its_provider_peers(
        client, ctx, monkeypatch):
    from aiflow.v4.control import incidents
    from aiflow.v4.control.loops import watchdog
    from aiflow.v4.core.controlrepo import Account

    notifications = []
    monkeypatch.setattr(incidents.notify, "send", lambda *args, **kwargs: notifications.append(args) or ["email"])
    # Seed both accounts before joining: join starts asynchronous readiness checks, which must see the peer's
    # credential baseline before candidate routing treats that peer as available.
    ctx.repo().accounts.append(Account(id="codex-02", provider="codex", node="node1", tier="middle", roles=["dev"]))
    client.app.state.sync_accounts()
    headers = join(client, "node1")
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=headers)
    for thread in list(threading.enumerate()):
        if thread.name == "aiflow4-heartbeat-readiness-node1":
            thread.join(timeout=15)
    add_ticket(ctx.db, "TESTWP-QUOTA-1")
    now = datetime.now(timezone.utc)
    quota = {"windows": {"five_hour": {"utilization": .8,
                                         "resets_at": (now + timedelta(hours=3)).isoformat()}}}
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='run',quota_observed_at=now() WHERE id='codex-01'",
               (J(quota),))
    for index in range(20):
        started = now - timedelta(hours=2) + timedelta(minutes=index * 5)
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
                    "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
                    " VALUES (%s,%s,'TESTWP','dev','codex-01','codex','gpt-6-luna','node1',%s,'finished','done',"
                    "%s,%s,1000,0,.01)",
                    (str(uuid.uuid4()), f"TESTWP-HIST-{index}", index + 1,
                     started, started + timedelta(minutes=1)))

    watchdog(ctx)
    assert ctx.db.one("SELECT quota_at_risk FROM accounts WHERE id='codex-01'")["quota_at_risk"] is True
    assert client.get("/v1/work/candidates?account=codex-01", headers=headers).json()["tickets"] == []
    assert [ticket["key"] for ticket in client.get("/v1/work/candidates?account=codex-02",
                                                   headers=headers).json()["tickets"]] == ["TESTWP-QUOTA-1"]
    incident = ctx.db.one("SELECT severity,title,notified FROM incidents WHERE kind='quota_prediction' AND subject='codex-01'"
                          " AND closed_at IS NULL")
    assert incident["severity"] == "warning" and "run out before reset" in incident["title"]
    assert incident["notified"] is True
    assert any("codex-01 may run out before reset" in call[1] for call in notifications)

    # A replenished report clears the forecast and makes that account eligible again.
    quota["windows"]["five_hour"]["utilization"] = .1
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='run',quota_observed_at=now() WHERE id='codex-01'",
               (J(quota),))
    watchdog(ctx)
    assert ctx.db.one("SELECT quota_at_risk FROM accounts WHERE id='codex-01'")["quota_at_risk"] is False
    assert [ticket["key"] for ticket in client.get("/v1/work/candidates?account=codex-01",
                                                   headers=headers).json()["tickets"]] == ["TESTWP-QUOTA-1"]
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='quota_prediction' AND subject='codex-01'"
                      " AND closed_at IS NULL")["n"] == 0


def test_at_risk_account_falls_back_to_its_own_work_when_no_peer_is_available(client, ctx, monkeypatch):
    headers = join(client, "cc")
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=headers)
    add_ticket(ctx.db, "TESTWP-QUOTA-SOLO")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='claude-01'")
    # Routing reads the watchdog's stored flag and must not rebuild the historical quota assessment per poll.
    monkeypatch.setattr(control_api.quota_reporting, "account_assessment",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("history queried per poll")))

    tickets = client.get("/v1/work/candidates?account=claude-01", headers=headers).json()["tickets"]

    assert [ticket["key"] for ticket in tickets] == ["TESTWP-QUOTA-SOLO"]


def test_quota_exhaustion_routes_ticket_to_peer_and_returns_it_after_reset(client, ctx):
    from aiflow.v4.control.loops import watchdog
    from aiflow.v4.core.controlrepo import Account

    headers = join(client, "cc")
    ctx.repo().accounts.append(Account(id="claude-02", provider="claude", node="cc", tier="senior", roles=["dev"]))
    client.app.state.sync_accounts()
    report_account_credentials(ctx, "cc", {"claude-02": {"present": True, "valid": True}})
    add_ticket(ctx.db, "TESTWP-QUOTA-RESET")

    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-QUOTA-RESET", "account": "claude-01"},
                          headers=headers).json()
    reset_at = datetime.now(timezone.utc) + timedelta(hours=1)
    result = client.post(f"/v1/runs/{claimed['run_id']}/result", json={
        "fence": claimed["fence"], "outcome": "quota", "error_kind": "quota", "reset_at": reset_at.isoformat(),
        "quota": {"windows": {"five_hour": {"utilization": 1.0, "resets_at": reset_at.isoformat()}}},
    }, headers=headers)
    assert result.status_code == 200
    assert client.post(f"/v1/runs/{claimed['run_id']}/release", json={"fence": claimed["fence"]},
                       headers=headers).json()["released"] is True

    assert client.get("/v1/work/candidates?account=claude-01", headers=headers).json()["tickets"] == []
    peer_response = client.get("/v1/work/candidates?account=claude-02", headers=headers).json()
    peer_tickets = peer_response["tickets"]
    assert [ticket["key"] for ticket in peer_tickets] == ["TESTWP-QUOTA-RESET"], peer_response
    peer_claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-QUOTA-RESET", "account": "claude-02"},
                             headers=headers).json()
    assert ctx.db.one("SELECT account_id FROM runs WHERE id=%s", (peer_claim["run_id"],))["account_id"] == "claude-02"
    assert client.post(f"/v1/runs/{peer_claim['run_id']}/result",
                       json={"fence": peer_claim["fence"], "outcome": "blocked"}, headers=headers).status_code == 200
    assert client.post(f"/v1/runs/{peer_claim['run_id']}/release", json={"fence": peer_claim["fence"]},
                       headers=headers).json()["released"] is True

    recovered_quota = {"windows": {"five_hour": {"utilization": 0.0,
                                                    "resets_at": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()}}}
    recovered = client.post("/v1/accounts/claude-01/probe", json={"ok": True, "quota": recovered_quota},
                            headers=headers)
    assert recovered.status_code == 200
    watchdog(ctx)

    returned_tickets = client.get("/v1/work/candidates?account=claude-01", headers=headers).json()["tickets"]
    assert [ticket["key"] for ticket in returned_tickets] == ["TESTWP-QUOTA-RESET"]


def test_grok_quota_refresh_is_unsupported_and_does_not_change_account_health(client, ctx, tmp_path, monkeypatch):
    from aiflow.v4.control.loops import watchdog
    from aiflow.v4.core.controlrepo import Account
    from aiflow.v4.worker import loop as worker_loop

    ctx.repo().accounts.append(Account(id="grok-01", provider="grok", node="cc", tier="middle", roles=["dev"]))
    client.app.state.sync_accounts()
    auth = join(client, "cc")
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2}, headers=auth)

    # Start with the incident state produced by a real sign-in failure. A good streamed answer must clear it.
    failed = client.post("/v1/accounts/grok-01/probe", json={
        "ok": False, "error_kind": "auth", "summary": "not signed in; please run /login"}, headers=auth)
    assert failed.status_code == 200
    assert ctx.db.one("SELECT status FROM accounts WHERE id='grok-01'")["status"] == "signed_out"
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='account_quota' AND subject='grok-01'"
                      " AND closed_at IS NULL")["n"] == 1

    monkeypatch.setattr(worker_loop.Worker, "publish", lambda self, **fields: self.state.update(fields))
    worker = worker_loop.Worker("grok-01", "http://testserver", "node-token", "cc", str(tmp_path), run_as=[])

    def call_api(method, path, json=None, params=None, allow_not_found=False):
        response = client.request(method, path, json=json, params=params, headers=auth)
        if allow_not_found and response.status_code == 404:
            return None
        assert response.status_code == 200, response.text
        return response.json()

    worker.api.call = call_api
    assert worker.refresh_quota() is False
    assert ctx.db.one("SELECT status FROM accounts WHERE id='grok-01'")["status"] == "signed_out"
    job = ctx.db.one("SELECT last_error,last_success_at FROM quota_refresh_jobs WHERE account_id='grok-01'")
    assert "no read-only quota command" in job["last_error"] and job["last_success_at"] is None
    assert ctx.db.one("SELECT count(*) AS n FROM account_quota_samples WHERE account_id='grok-01'")["n"] == 0
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='account_quota' AND subject='grok-01'"
                      " AND closed_at IS NULL")["n"] == 1


def test_verify_goes_to_another_account_and_merges(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-20")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "claude-01"}, headers=cc).json()
    pr = client.post("/v1/github/pr", json={"run_id": r["run_id"], "fence": r["fence"], "repo": "test-aiflow-wp",
                                            "head": "TESTWP-20-x", "base": "develop", "title": "TESTWP-20: x"}, headers=cc).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "pr_url": pr["url"],
                                                        "branch": "TESTWP-20-x", "summary": "built it"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-20'")
    # the developer's own account is not offered the verification; another account is
    assert [t["step"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]] == []
    cands = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1).json()["tickets"]
    assert cands == [{"key": "TESTWP-20", "summary": "TESTWP-20", "priority": "Medium", "labels": ["aiflow-ready"], "step": "verify"}] or \
        (cands[0]["key"], cands[0]["step"]) == ("TESTWP-20", "verify")
    v = client.post("/v1/work/claim", json={"ticket": "TESTWP-20", "account": "codex-01", "step": "verify"}, headers=n1).json()
    assert v["claimed"] and v["pr"]["number"] == 1 and v["pr"]["branch"] == "TESTWP-20-x" and v["pr"]["dev_account"] == "claude-01"
    # a verify run cannot edit files through the hook
    hook = {"tool_name": "Write", "tool_input": {"file_path": "/srv/aiflow/work/x/a.php"}, "cwd": "/srv/aiflow/work/x"}
    assert client.post("/v1/policy/decide", json=hook, headers={"Authorization": f"Bearer {v['run_token']}"}).json()[
        "hookSpecificOutput"]["permissionDecision"] == "deny"
    m = client.post("/v1/github/merge", json={"run_id": v["run_id"], "fence": v["fence"], "number": 1, "title": "TESTWP-20: x"},
                    headers=n1).json()
    assert m["merged"] and ctx.github.merged[0][1] == 1
    # a dev run may not merge
    assert client.post("/v1/github/merge", json={"run_id": r["run_id"], "fence": r["fence"], "number": 1}, headers=cc).status_code in (403, 409)


def test_triage_candidates_follow_verify_and_exclude_last_dev_and_escalated(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-63", status="Needs owner", labels=("aiflow-ready",))
    add_open_escalation(ctx.db, "TESTWP-63")
    add_ticket(ctx.db, "TESTWP-64", status="Needs owner", labels=("owner-escalated",))
    add_open_escalation(ctx.db, "TESTWP-64")
    add_ticket(ctx.db, "TESTWP-65", status="In Verify")
    add_ticket(ctx.db, "TESTWP-66", status="To Do")
    add_ticket(ctx.db, "TESTWP-67", status="Needs owner")
    add_open_escalation(ctx.db, "TESTWP-67")

    def add_run(key, step, account, outcome, summary, pr_url="", details=None):
        run_id = str(uuid.uuid4())
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                    "summary,pr_url,details) VALUES (%s,%s,'TESTWP',%s,%s,%s,%s,1,'finished',%s,%s,%s,%s)",
                    (run_id, key, step, account, "claude" if account.startswith("claude") else "codex",
                     "cc" if account.startswith("claude") else "node1", outcome, summary, pr_url, J(details or {})))
        return run_id

    add_run("TESTWP-63", "dev", "claude-01", "blocked", "Can we change the API response shape?")
    add_run("TESTWP-63", "verify", "codex-01", "rework", "verify failed", details={"findings": "- add a nonce"})
    add_run("TESTWP-65", "dev", "claude-01", "done", "Implemented it", "https://github.com/x/y/pull/9")
    add_run("TESTWP-65", "verify", "claude-01", "rework", "verify failed", details={"findings": "- add a nonce"})
    ctx.jira.recent_comment_data["TESTWP-63"] = [
        {"created": "2026-10-20T12:00:00Z", "author": "Owner", "body": "Keep the response backwards compatible."}
    ]

    # The developer account cannot triage its own question.
    claude = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]
    assert not any(t["key"] == "TESTWP-63" and t["step"] == "triage" for t in claude)
    assert any(t["key"] == "TESTWP-67" and t["step"] == "triage" for t in claude)

    candidates = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1).json()["tickets"]
    assert [(t["key"], t["step"]) for t in candidates] == [
        ("TESTWP-65", "verify"), ("TESTWP-63", "triage"), ("TESTWP-67", "triage"), ("TESTWP-66", "dev")]
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-63", "account": "codex-01", "step": "triage"},
                          headers=n1).json()
    assert claimed["claimed"] and claimed["step"] == "triage"
    assert claimed["last_dev_summary"] == "Can we change the API response shape?"
    assert claimed["verify_findings"] == "- add a nonce"
    assert claimed["recent_comments"] == ctx.jira.recent_comment_data["TESTWP-63"]

    # Triage receives a read-only GitHub token.
    token = client.post("/v1/github/token", json={"run_id": claimed["run_id"], "fence": claimed["fence"],
                                                  "repo": "jooservices/test-aiflow-wp"}, headers=n1)
    assert token.status_code == 200
    assert ctx.github.token_requests[-1][1] == {"contents": "read", "metadata": "read"}
    auth = {"Authorization": f"Bearer {claimed['run_token']}"}
    assert client.post("/v1/policy/decide", json={"tool_name": "Bash", "tool_input": {"command": "git status"},
                                                  "cwd": "/srv/work/TESTWP"}, headers=auth).json()[
        "hookSpecificOutput"]["permissionDecision"] == "allow"
    for tool, tool_input in (("Bash", {"command": "touch /tmp/changed"}),
                             ("Bash", {"command": "cat ticket.txt > /tmp/copy"}),
                             ("Bash", {"command": "find . -exec touch changed \\;"}),
                             ("Bash", {"command": "git branch -f develop"}),
                             ("Write", {"file_path": "/srv/work/TESTWP/changed.py"})):
        hook = {"tool_name": tool, "tool_input": tool_input, "cwd": "/srv/work/TESTWP"}
        assert client.post("/v1/policy/decide", json=hook, headers=auth).json()[
            "hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ticket_in_needs_owner_without_an_open_escalation_is_not_offered_to_triage(client, ctx):
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-80", status="Needs owner")
    add_ticket(ctx.db, "TESTWP-81", status="Needs owner")
    add_open_escalation(ctx.db, "TESTWP-81")

    keys = [t["key"] for t in client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1)
            .json()["tickets"] if t["step"] == "triage"]

    assert keys == ["TESTWP-81"]


def test_failed_triage_backs_off_before_the_ticket_is_offered_again(client, ctx):
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-67", status="Needs owner")
    add_open_escalation(ctx.db, "TESTWP-67")

    def triage_keys():
        return [t["key"] for t in client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1)
                .json()["tickets"] if t["step"] == "triage"]

    assert triage_keys() == ["TESTWP-67"]
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-67", "account": "codex-01", "step": "triage"},
                    headers=n1).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={
        "fence": r["fence"], "outcome": "failed",
        "summary": "triage returned no valid DECISION: ANSWER or DECISION: ESCALATE response"}, headers=n1)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=n1)
    assert triage_keys() == []
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-67", "account": "codex-01", "step": "triage"},
                       headers=n1).status_code == 409
    ctx.db.run("UPDATE runs SET ended_at = now() - interval '40 minutes' WHERE ticket_key='TESTWP-67'")
    assert triage_keys() == ["TESTWP-67"]


def test_triage_answer_reaches_the_next_dev_prompt(client, ctx):
    from aiflow.v4.worker.dev import Claimed, build_prompt
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-68")
    d = client.post("/v1/work/claim", json={"ticket": "TESTWP-68", "account": "claude-01"}, headers=cc).json()
    client.post(f"/v1/runs/{d['run_id']}/result", json={
        "fence": d["fence"], "outcome": "blocked", "branch": "TESTWP-68-x",
        "summary": "Which Redis database should the cache use?"}, headers=cc)
    client.post(f"/v1/runs/{d['run_id']}/release", json={"fence": d["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='Needs owner' WHERE key='TESTWP-68'")
    add_open_escalation(ctx.db, "TESTWP-68", "Which Redis database should the cache use?")
    t = client.post("/v1/work/claim", json={"ticket": "TESTWP-68", "account": "codex-01", "step": "triage"},
                    headers=n1).json()
    assert t["claimed"]
    client.post(f"/v1/runs/{t['run_id']}/result", json={
        "fence": t["fence"], "outcome": "answered", "summary": "Use database 2.",
        "details": {"decision": "ANSWER", "answer": "Use Redis database 2, as deploy/v4 configures."}}, headers=n1)
    client.post(f"/v1/runs/{t['run_id']}/release", json={"fence": t["fence"]}, headers=n1)
    ctx.db.run("UPDATE tickets SET status='To Do' WHERE key='TESTWP-68'")

    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-68", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["triage_answer"] == "Use Redis database 2, as deploy/v4 configures."
    assert r["triage_question"] == "Which Redis database should the cache use?"
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account="claude-01",
                provider=r["provider"], model=r["model"], effort=r["effort"], ticket=r["ticket"], project=r["project"],
                identity=tuple(r["identity"]), branch=r["branch"], triage_answer=r["triage_answer"],
                triage_question=r["triage_question"])
    prompt = build_prompt(c, c.branch)
    assert "Use Redis database 2, as deploy/v4 configures." in prompt
    assert "Which Redis database should the cache use?" in prompt

    def next_dev(outcome):
        nonlocal r
        client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": outcome}, headers=cc)
        client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
        ctx.db.run("UPDATE runs SET ended_at = now() - interval '40 minutes' WHERE ticket_key='TESTWP-68'")
        ctx.db.run("UPDATE tickets SET status='To Do' WHERE key='TESTWP-68'")
        r = client.post("/v1/work/claim", json={"ticket": "TESTWP-68", "account": "claude-01"}, headers=cc).json()
        return r

    assert next_dev("failed")["triage_answer"] == "Use Redis database 2, as deploy/v4 configures."  # survives a crash
    assert "triage_answer" not in next_dev("done")  # used once a dev run finished with it


def test_rework_findings_reach_the_next_dev_run(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-21")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-21", "account": "claude-01"}, headers=cc).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "branch": "TESTWP-21-y",
                                                        "pr_url": "https://github.com/jooservices/test-aiflow-wp/pull/7"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-21'")
    v = client.post("/v1/work/claim", json={"ticket": "TESTWP-21", "account": "codex-01", "step": "verify"}, headers=n1).json()
    client.post(f"/v1/runs/{v['run_id']}/result", json={"fence": v["fence"], "outcome": "rework", "summary": "verify failed",
                                                        "details": {"findings": "- core/x.php: missing nonce check"}}, headers=n1)
    client.post(f"/v1/runs/{v['run_id']}/release", json={"fence": v["fence"]}, headers=n1)
    ctx.db.run("UPDATE tickets SET status='To Do', labels=array['aiflow-ready','rework'] WHERE key='TESTWP-21'")
    d = client.post("/v1/work/claim", json={"ticket": "TESTWP-21", "account": "claude-01"}, headers=cc).json()
    assert d["claimed"] and d["findings"] == "- core/x.php: missing nonce check" and d["branch"] == "TESTWP-21-y"
    # a dev run that delivers nothing (blocked, no changes) does not answer the findings: the next one gets them too
    client.post(f"/v1/runs/{d['run_id']}/result", json={"fence": d["fence"], "outcome": "blocked", "branch": "TESTWP-21-y",
                                                        "summary": "the shell could not start"}, headers=cc)
    client.post(f"/v1/runs/{d['run_id']}/release", json={"fence": d["fence"]}, headers=cc)
    e = client.post("/v1/work/claim", json={"ticket": "TESTWP-21", "account": "claude-01"}, headers=cc).json()
    assert e["claimed"] and e["findings"] == "- core/x.php: missing nonce check" and e["branch"] == "TESTWP-21-y"
    # the owner sends it back to review after a run without changes: the delivered PR is reviewed
    client.post(f"/v1/runs/{e['run_id']}/result", json={"fence": e["fence"], "outcome": "failed",
                                                        "summary": "no changes: already done"}, headers=cc)
    client.post(f"/v1/runs/{e['run_id']}/release", json={"fence": e["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-21'")
    cands = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1).json()["tickets"]
    assert ("TESTWP-21", "verify") in [(t["key"], t["step"]) for t in cands]


def test_node_without_ai_accounts_is_flagged(client, ctx):
    from aiflow.v4.control.loops import watchdog
    n3 = join(client, "node3")
    client.post("/v1/nodes/node3/heartbeat", json={"slots_total": 2}, headers=n3)
    watchdog(ctx)
    inc = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='node_no_ai' AND subject='node3' AND closed_at IS NULL")
    assert inc["severity"] == "warning" and "no AI account" in inc["title"] and "not described" in inc["detail"]
    assert "Machines and AI screen" in inc["detail"] and "accounts.yaml" not in inc["detail"]
    s = client.get("/v1/state").json()
    n = next(x for x in s["nodes"] if x["name"] == "node3")
    assert n["accounts_assigned"] == 0 and n["in_control_repo"] is False


def test_failing_ticket_backs_off_and_is_reported(client, ctx):
    from aiflow.v4.control.loops import watchdog
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-30")
    for i in range(3):
        ctx.db.run("UPDATE tickets SET status='To Do' WHERE key='TESTWP-30'")
        cands = [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]]
        if i == 0:
            assert cands == ["TESTWP-30"]
        r = client.post("/v1/work/claim", json={"ticket": "TESTWP-30", "account": "claude-01"}, headers=cc)
        if i > 0:
            assert r.status_code == 409  # backing off after a failure
            ctx.db.run("UPDATE runs SET ended_at = now() - interval '40 minutes' WHERE ticket_key='TESTWP-30'")
            r = client.post("/v1/work/claim", json={"ticket": "TESTWP-30", "account": "claude-01"}, headers=cc)
        r = r.json()
        client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "failed", "summary": "worker error: x"}, headers=cc)
        client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
    assert ctx.jira.worklogs and ctx.jira.worklogs[0][0] == "TESTWP-30" and ctx.jira.worklogs[0][1] >= 60
    ctx.db.run("UPDATE runs SET ended_at = now() WHERE ticket_key='TESTWP-30'")
    watchdog(ctx)
    assert ctx.db.one("SELECT title FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL")["title"] == "TESTWP-30 failed 3 runs in a row"


def test_role_with_one_ready_account_is_reported(client, ctx):
    from aiflow.v4.control.loops import watchdog
    for name in ("cc", "node1"):
        client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2}, headers=join(client, name))
    ctx.db.run("UPDATE accounts SET status='idle'")
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='role_coverage' AND subject='verify' AND closed_at IS NULL")["n"] == 0
    ctx.db.run("UPDATE accounts SET status='signed_out' WHERE id='codex-01'")
    watchdog(ctx)
    inc = ctx.db.one("SELECT severity, title FROM incidents WHERE kind='role_coverage' AND subject='verify' AND closed_at IS NULL")
    assert inc["severity"] == "warning" and inc["title"] == "role verify has 1 ready provider"


def test_state_reports_registration_and_last_seen_for_an_offline_node(client, ctx):
    from datetime import datetime, timedelta, timezone

    from aiflow.v4.control.state import NODE_OFFLINE_S

    node = join(client, "node3", ip="192.0.2.55")
    client.post("/v1/nodes/node3/heartbeat", json={"slots_total": 2, "metrics": {"hostname": "worker-3"}}, headers=node)
    n = next(x for x in client.get("/v1/state").json()["nodes"] if x["name"] == "node3")
    assert n["ip"] == "192.0.2.55"
    assert n["status"] == "online"
    first_seen = datetime.fromisoformat(n["last_heartbeat"])
    assert first_seen.tzinfo is not None and n["heartbeat_age_s"] < 5

    # no more heartbeats: the node turns offline but keeps the IP and the exact time it was last seen
    last = datetime.now(timezone.utc) - timedelta(seconds=NODE_OFFLINE_S + 5)
    ctx.db.run("UPDATE nodes SET last_heartbeat=%s WHERE name='node3'", (last,))
    n = next(x for x in client.get("/v1/state").json()["nodes"] if x["name"] == "node3")
    assert n["status"] == "offline"
    assert n["ip"] == "192.0.2.55"
    assert datetime.fromisoformat(n["last_heartbeat"]) == last
    assert n["heartbeat_age_s"] >= NODE_OFFLINE_S


def test_restart_sync_keeps_join_reported_ip_and_last_heartbeat(client, ctx):
    from fastapi.testclient import TestClient

    from aiflow.v4.control.api import create_app

    assert ctx.repo().nodes["cc"].ip == "10.44.0.10"
    node = join(client, "cc", ip="192.0.2.77")
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "metrics": {"hostname": "cc-worker"}}, headers=node)
    before = next(x for x in client.get("/v1/state").json()["nodes"] if x["name"] == "cc")
    assert before["ip"] == "192.0.2.77" and before["status"] == "online"

    # an API restart runs the startup sync again; the join-reported address and last heartbeat must survive
    with TestClient(create_app(ctx)) as restarted:
        after = next(x for x in restarted.get("/v1/state").json()["nodes"] if x["name"] == "cc")
    assert after["ip"] == "192.0.2.77"
    assert after["last_heartbeat"] == before["last_heartbeat"]
    assert after["metrics"]["hostname"] == "cc-worker"


def test_project_report_summarizes_seeded_project_and_epic_history(client, ctx):
    add_ticket(ctx.db, "TESTWP-100", status="Done", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic', jira_created=%s, jira_updated=%s, done_at=%s WHERE key='TESTWP-100'",
               (datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 8, tzinfo=timezone.utc),
                datetime(2025, 1, 8, tzinfo=timezone.utc)))
    created = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i, days in enumerate((7, 6, 5, 4, 3, 2), start=101):
        add_ticket(ctx.db, f"TESTWP-{i}", status="Done", labels=())
        ctx.db.run("UPDATE tickets SET type=%s, parent_key='TESTWP-100', jira_created=%s, jira_updated=%s, done_at=%s"
                   " WHERE key=%s", ("Story" if i % 2 else "Task", created, created + timedelta(days=days),
                                      created + timedelta(days=days), f"TESTWP-{i}"))

    runs = [
        ("TESTWP-101", "dev", "claude-01", "claude", "opus", "done", 100, 40, .10, 100),
        ("TESTWP-101", "verify", "codex-01", "codex", "gpt-6-luna", "rework", 20, 5, .02, 20),
        ("TESTWP-101", "dev", "claude-01", "claude", "opus", "done", 50, 25, .05, 50),
        ("TESTWP-102", "dev", "codex-01", "codex", "gpt-6-luna", "done", 30, 10, .03, 40),
    ]
    for ticket, step, account, provider, model, outcome, tin, tout, cost, duration in runs:
        started = created + timedelta(days=1)
        ctx.db.run(
            "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
            "started_at,ended_at,tokens_in,tokens_out,cost_usd)"
            " VALUES (%s,%s,'TESTWP',%s,%s,%s,%s,'cc',1,'finished',%s,%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), ticket, step, account, provider, model, outcome, started,
             started + timedelta(seconds=duration), tin, tout, cost))
    ctx.db.run("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
               " VALUES ('jira','transition','TESTWP-102','codex-01',true,'Needs owner',%s)",
               (J({"to": "Needs owner"}),))
    ctx.db.run("INSERT INTO incidents (severity,kind,subject,title,opened_at)"
               " VALUES ('warning','ticket_stuck','TESTWP-101','TESTWP-101 failed repeatedly',%s)",
               (created + timedelta(days=2),))
    ctx.db.run("INSERT INTO incidents (severity,kind,subject,title)"
               " VALUES ('warning','account_quota','claude-01','account out of quota')")

    response = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-100"})
    assert response.status_code == 200
    report = response.json()
    assert report["period"] == {
        "start": created.isoformat(), "end": (created + timedelta(days=7)).isoformat(), "days": 7.0,
    }
    assert report["tickets"] == {"total": 6, "done": 6, "in_progress": 0, "needs_owner": 0,
                                  "done_by_type": {"Story": 3, "Task": 3}}
    assert report["lead_time"] == {"sample_size": 6, "median_seconds": 388800.0, "p90_seconds": 604800.0}
    assert report["ai_time"] == {"sample_size": 2, "median_seconds": 105.0, "p90_seconds": 170.0,
                                  "total_seconds": 210.0}
    assert report["runs_by_outcome"] == {"done": 3, "rework": 1}
    assert report["rework_rounds"] == {"count": 1, "tickets": [{"key": "TESTWP-101", "rounds": 1}]}
    usage = report["usage"]
    assert (usage["tokens_in"], usage["tokens_out"], usage["tokens_total"], usage["cost_usd"]) == (200, 80, 280, .2)
    by_account = {row["account"]: row for row in usage["by_account"]}
    assert by_account["claude-01"] == {"account": "claude-01", "runs": 2, "tokens_in": 150,
                                        "tokens_out": 65, "tokens_total": 215, "cost_usd": .15}
    by_model = {(row["provider"], row["model"]): row for row in usage["by_model"]}
    assert by_model[("codex", "gpt-6-luna")]["tokens_total"] == 65
    assert report["incidents_opened"]["count"] == 1
    assert report["incidents_opened"]["items"][0]["ticket"] == "TESTWP-101"
    assert report["owner_needed"] == {"count": 1, "tickets": [{"key": "TESTWP-102", "summary": "Summary of TESTWP-102"}]}
    assert [r["key"] for r in report["slowest_tickets"]] == [
        "TESTWP-101", "TESTWP-102", "TESTWP-103", "TESTWP-104", "TESTWP-105"]

    whole_project = client.get("/v1/projects/TESTWP/report").json()
    assert whole_project["tickets"]["done"] == 6
    markdown = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-100", "format": "md"})
    assert markdown.status_code == 200 and markdown.headers["content-type"].startswith("text/markdown")
    assert "# Progress report: TESTWP-100" in markdown.text
    assert "| Story | 3 |" in markdown.text and "median 4.5d, p90 7.0d (n=6)" in markdown.text
    assert "Tokens: 280 (200 in, 80 out)" in markdown.text and "TESTWP-102 · Summary of TESTWP-102" in markdown.text
    assert client.get("/v1/projects/UNKNOWN/report").status_code == 404
    assert client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-101"}).status_code == 404
    printable = client.get("/v1/projects/TESTWP/report", params={"format": "html"})
    assert printable.status_code == 200 and printable.headers["content-type"].startswith("text/html")


def test_team_lead_epic_requirement_coverage_progress_and_exports(client, ctx):
    created = datetime(2025, 3, 1, tzinfo=timezone.utc)
    done = created + timedelta(days=5)
    add_ticket(ctx.db, "TESTWP-700", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic', jira_created=%s WHERE key='TESTWP-700'", (created,))
    children = [
        ("TESTWP-701", "Done", "Acceptance criteria:\n- AC1: Create row\n- AC2: Render <script>alert(1)</script>\n"
          "Technical notes:\n- AC3: This is outside the AC section", "TESTWP-700"),
        ("TESTWP-702", "In Progress", "Acceptance criteria:\n- AC1: Show status", "TESTWP-700"),
        ("TESTWP-703", "Needs owner", "No acceptance criteria section", "TESTWP-700"),
        ("TESTWP-704", "Done", "Acceptance criteria:\n- AC1: Validate input", "TESTWP-700"),
    ]
    for key, status, description, parent in children:
        add_ticket(ctx.db, key, status=status, labels=())
        ctx.db.run("UPDATE tickets SET type='Story', parent_key=%s, description=%s, jira_created=%s, jira_updated=%s,"
                   " done_at=%s WHERE key=%s", (parent, description, created, done, done if status == "Done" else None, key))
    ctx.db.run("UPDATE tickets SET summary=%s WHERE key='TESTWP-702'", ('=HYPERLINK("https://example.invalid")',))

    def run(key, step, outcome, tokens_in, tokens_out, cost, details=None):
        ctx.db.run(
            "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
            "started_at,ended_at,tokens_in,tokens_out,cost_usd,details)"
            " VALUES (%s,%s,'TESTWP',%s,'codex-01','codex','gpt-6-luna','cc',1,'finished',%s,%s,%s,%s,%s,%s,%s)",
            (str(uuid.uuid4()), key, step, outcome, created, created + timedelta(seconds=60), tokens_in, tokens_out,
             cost, J(details or {})))

    run("TESTWP-701", "verify", "merged", 100, 20, .10, {"verdict": "PASS"})
    run("TESTWP-702", "dev", "done", 50, 25, .05)
    # A PASS review followed by failed CI is recorded as rework; Jira Done must not make its AC look verified.
    run("TESTWP-704", "verify", "rework", 30, 10, .02,
        {"verdict": "PASS", "findings": "CI failed after the verifier passed"})

    response = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-700"})
    assert response.status_code == 200
    report = response.json()
    assert report["tickets"] == {"total": 4, "done": 2, "in_progress": 1, "needs_owner": 1,
                                  "done_by_type": {"Story": 2}}
    coverage = report["requirement_coverage"]
    assert coverage["insufficient_tasks"] is True
    by_key = {t["key"]: t for t in coverage["tickets"]}
    assert by_key["TESTWP-701"]["parent_key"] == "TESTWP-700"
    assert [(ac["id"], ac["status"]) for ac in by_key["TESTWP-701"]["acceptance_criteria"]] == [
        ("AC1", "verified"), ("AC2", "verified")]
    assert "AC3" not in {ac["id"] for ac in by_key["TESTWP-701"]["acceptance_criteria"]}
    assert by_key["TESTWP-704"]["acceptance_criteria"][0]["status"] == "not_confirmed"
    assert {(g["ticket"], g["reason"]) for g in coverage["gaps"]} == {
        ("TESTWP-703", "ticket_has_no_acceptance_criteria"),
        ("TESTWP-704", "done_without_verified_acceptance_criteria"),
    }
    assert report["rework_rounds"]["count"] == 1
    assert report["lead_time"]["sample_size"] == 2
    assert report["usage"]["tokens_total"] == 235 and report["usage"]["cost_usd"] == .17

    markdown = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-700", "format": "md"})
    assert markdown.status_code == 200 and "attachment" in markdown.headers["content-disposition"]
    assert "Done: 2 of 4" in markdown.text and "AC1 [verified]: Create row" in markdown.text
    csv_report = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-700", "format": "csv"})
    assert csv_report.status_code == 200 and csv_report.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(csv_report.text)))
    assert len(rows) == 4 and rows[0]["epic"] == "TESTWP-700"
    assert "AC1 [verified]: Create row" in rows[0]["acceptance_criteria"]
    assert rows[1]["summary"].startswith("'=")
    assert rows[-1]["coverage_gap"] == "done_without_verified_acceptance_criteria"

    printable = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-700", "format": "html"})
    assert printable.status_code == 200 and "@media print" in printable.text
    assert "Insufficient tasks found" in printable.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in printable.text
    assert "<script>alert(1)</script>" not in printable.text

    add_ticket(ctx.db, "TESTWP-710", status="To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-710'")
    empty = client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-710"}).json()
    assert empty["requirement_coverage"]["insufficient_tasks"] is True
    assert empty["requirement_coverage"]["gaps"] == [
        {"ticket": "TESTWP-710", "reason": "epic_has_no_child_tickets"}]


def test_jira_sync_uses_resolution_date_for_done_ticket_insert_and_keeps_it_on_edits(ctx):
    from aiflow.v4.control.jira import FIELDS, Jira
    from aiflow.v4.control.loops import jira_sync

    completed = datetime(2025, 2, 3, 12, tzinfo=timezone.utc)
    issue = {"key": "TESTWP-501", "fields": {
        "summary": "Already complete", "status": {"name": "Done"}, "issuetype": {"name": "Task"},
        "created": "2025-02-01T12:00:00.000+0000", "updated": "2025-02-05T12:00:00.000+0000",
        "resolutiondate": "2025-02-03T12:00:00.000+0000",
    }}
    ctx.jira.search = lambda _jql: [issue]
    ctx.jira.normalize = Jira.normalize

    assert "resolutiondate" in FIELDS
    assert jira_sync(ctx, full=True) == 1
    row = ctx.db.one("SELECT status, jira_updated, done_at FROM tickets WHERE key='TESTWP-501'")
    assert row["status"] == "Done"
    assert row["jira_updated"] == datetime(2025, 2, 5, 12, tzinfo=timezone.utc)
    assert row["done_at"] == completed

    issue["fields"]["updated"] = "2025-02-10T12:00:00.000+0000"
    assert jira_sync(ctx, full=True) == 1
    row = ctx.db.one("SELECT jira_updated, done_at FROM tickets WHERE key='TESTWP-501'")
    assert row["jira_updated"] == datetime(2025, 2, 10, 12, tzinfo=timezone.utc)
    assert row["done_at"] == completed


def test_project_report_markdown_escapes_jira_text(client, ctx):
    add_ticket(ctx.db, "TESTWP-510", status="Done", labels=())
    created = datetime(2025, 1, 1, tzinfo=timezone.utc)
    done = datetime(2025, 1, 2, tzinfo=timezone.utc)
    summary = "Slow | title\n\n## Injected\n![image](https://attacker.test) <img src=x> ~~hidden~~"
    ctx.db.run("UPDATE tickets SET summary=%s, jira_created=%s, jira_updated=%s, done_at=%s WHERE key='TESTWP-510'",
               (summary, created, done, done))
    ctx.db.run("INSERT INTO incidents (severity,kind,subject,title,opened_at) VALUES ('warning','test','TESTWP-510',%s,%s)",
               ("Problem\n\n## Forged heading\n[link](https://attacker.test)", done))

    markdown = client.get("/v1/projects/TESTWP/report", params={"format": "md"}).text

    assert "\n\n## Injected" not in markdown
    assert "\n\n## Forged heading" not in markdown
    assert "Slow \\| title \\#\\# Injected \\!\\[image\\]\\(https://attacker.test\\) \\<img src=x\\> \\~\\~hidden\\~\\~" in markdown
    assert "Problem \\#\\# Forged heading \\[link\\]\\(https://attacker.test\\)" in markdown
    assert "![image](https://attacker.test)" not in markdown
    assert "<img src=x>" not in markdown


def test_node_back_online_resolves_and_alerts(client, ctx, monkeypatch):
    from aiflow.v4.control import notify
    from aiflow.v4.control.loops import watchdog
    sent = []
    monkeypatch.setattr(notify, "send", lambda sev, title, detail="", link="", **kwargs: sent.append((sev, title)) or ["test"])
    n1 = join(client, "node1")
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=n1)
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '5 minutes' WHERE name='node1'")
    watchdog(ctx)
    acc = next(a for a in client.get("/v1/state").json()["accounts"] if a["id"] == "codex-01")
    assert acc["status"] == "node_offline"
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=n1)
    assert ("critical", "node node1 is down") in sent and ("info", "resolved: node node1 is down") in sent


def test_parallel_sessions_of_one_account(client, ctx, tmp_path):
    from aiflow.v4.worker.loop import Worker
    w = Worker("codex-01.2", "http://x", "t", "node1", str(tmp_path), run_as=[])
    assert w.account == "codex-01" and w.instance == "codex-01.2"
    n1 = join(client, "node1")
    hb = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2, "workers": [
        {"account": "codex-01", "state": "idle", "alive": True}, {"account": "codex-01", "state": "working", "alive": True}]},
        headers=n1).json()
    assert hb["desired"]["workers"] == ["codex-01"]
    assert ctx.db.one("SELECT status FROM accounts WHERE id='codex-01'")["status"] == "working"


def test_in_progress_ticket_without_a_run_is_requeued(client, ctx):
    from aiflow.v4.control.loops import watchdog
    add_ticket(ctx.db, "TESTWP-40")
    ctx.db.run("UPDATE tickets SET status='In Progress', synced_at=now() WHERE key='TESTWP-40'")
    ctx.jira.status_of["TESTWP-40"] = "In Progress"
    watchdog(ctx)
    assert ctx.jira.status_of["TESTWP-40"] == "To Do"
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-40'")["status"] == "To Do"


def test_nodes_follow_the_current_release(client, ctx, tmp_path, monkeypatch):
    n1 = join(client, "node1")
    (tmp_path / "CURRENT").write_text("v4.2.1\n")
    monkeypatch.setattr(control_api, "RELEASES", tmp_path)
    hb = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=n1).json()
    assert hb["desired"]["release"] == ""
    upgraded = client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2, "release_protocol": 1},
                           headers=n1).json()
    assert upgraded["desired"]["release"] == "v4.2.1"
    assert client.get("/v1/releases/zzz.tar.gz", headers=n1).status_code in (400, 404)
    assert client.get("/v1/releases/abc1234.tar.gz", headers=n1).status_code == 404
    assert client.get("/v1/releases/abc1234.tar.gz").status_code == 401


def test_failing_verify_backs_off(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-41")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-41", "account": "claude-01"}, headers=cc).json()
    client.post(f"/v1/runs/{r['run_id']}/result", json={"fence": r["fence"], "outcome": "done", "branch": "TESTWP-41-z",
                                                        "pr_url": "https://github.com/jooservices/test-aiflow-wp/pull/9"}, headers=cc)
    client.post(f"/v1/runs/{r['run_id']}/release", json={"fence": r["fence"]}, headers=cc)
    ctx.db.run("UPDATE tickets SET status='In Verify' WHERE key='TESTWP-41'")
    v = client.post("/v1/work/claim", json={"ticket": "TESTWP-41", "account": "codex-01", "step": "verify"}, headers=n1).json()
    client.post(f"/v1/runs/{v['run_id']}/result", json={"fence": v["fence"], "outcome": "failed", "summary": "verify worker error: x"}, headers=n1)
    client.post(f"/v1/runs/{v['run_id']}/release", json={"fence": v["fence"]}, headers=n1)
    cands = client.get("/v1/work/candidates", params={"account": "codex-01"}, headers=n1).json()["tickets"]
    assert all(c["key"] != "TESTWP-41" for c in cands)


def test_worktree_sweep_names_finished_tickets_and_spent_run_checkouts(client, ctx):
    from tests.v4.conftest import add_ticket
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-1", "Done")
    add_ticket(ctx.db, "TESTWP-2", "In Progress")
    add_ticket(ctx.db, "TESTWP-3", "Done")
    ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence) VALUES"
               " (gen_random_uuid(), 'TESTWP-3', 'TESTWP', 'verify', 'claude-01', 'cc', 1)")
    dirs = ["TESTWP/TESTWP-1-feature", "TESTWP/TESTWP-1-feature--verify", "TESTWP/TESTWP-2-feature",
            "TESTWP/TESTWP-2--intake", "TESTWP/TESTWP-2--triage-abc", "TESTWP/TESTWP-3-feature", "TESTWP/probe",
            "TESTWP/../etc"]
    r = client.post("/v1/nodes/cc/worktrees/sweep", json={"dirs": dirs}, headers=cc)
    assert r.status_code == 200
    assert sorted(r.json()["remove"]) == ["TESTWP/TESTWP-1-feature", "TESTWP/TESTWP-1-feature--verify",
                                          "TESTWP/TESTWP-2--intake", "TESTWP/TESTWP-2--triage-abc"]
    assert client.post("/v1/nodes/node1/worktrees/sweep", json={"dirs": []}, headers=cc).status_code == 403
