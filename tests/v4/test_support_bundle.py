"""Support bundle contents, privacy gating, inventory review and ticket-run log collection."""
from __future__ import annotations

import io
import json
import logging
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aiflow.v4.control import support_bundle
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J
from aiflow.v4.worker.loop import attach_support_log

SESSION_SECRET = "test-support-bundle-session-secret-with-32-bytes"


def sign_in_owner(client, ctx, role="owner", subject="owner@example.test"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session(subject, role, 2_000_000_000, SESSION_SECRET))


def add_run(ctx, ticket, log_text, *, age_hours=0, step="dev"):
    run_id = uuid.uuid4()
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,details,started_at)"
               " VALUES (%s,%s,%s,%s,%s,%s,1,%s,now()-(%s * interval '1 hour'))",
               (run_id, ticket, ticket.split("-")[0], step, "codex-01", "node1", J({}), age_hours))
    support_bundle.store_run_log(ctx, str(run_id), log_text)
    return str(run_id)


def test_general_bundle_contains_diagnostics_and_caps_incidents_and_log_files(ctx):
    ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,slots_used,release_version,tools)"
               " VALUES ('node1','worker','online',4,2,'4.1.0',%s)",
               (J({"python": "3.12.4", "git": "git version 2"}),))
    ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
               " VALUES ('claude-01','claude','node1','senior',%s,'idle')", (["dev", "verify"],))
    for index in range(105):
        ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail,opened_at)"
                   " VALUES ('test',%s,'warning',%s,%s,now()-(%s * interval '1 minute'))",
                   (f"subject-{index}", f"Incident {index}", "Safe diagnostic detail", index))
    ctx.db.run("INSERT INTO incidents (kind,subject,title,opened_at) VALUES ('old','old','Old incident',"
               "now()-interval '8 days')")

    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, "items=1\n" * (support_bundle.MAX_LOG_BYTES // 7),
                                (), None))
    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=logs)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert {"versions.json", "settings.json", "machine-state.json", "account-state.json", "incidents.json",
                "logs/control-service.log", "inventory.json"} <= set(bundle.namelist())
        assert len(bundle.read("logs/control-service.log")) == support_bundle.MAX_LOG_BYTES
        versions = json.loads(bundle.read("versions.json"))
        settings = json.loads(bundle.read("settings.json"))
        machine = json.loads(bundle.read("machine-state.json"))[0]
        account = json.loads(bundle.read("account-state.json"))[0]
        incidents = json.loads(bundle.read("incidents.json"))
        assert versions["aiflow"] and versions["python"] and versions["nodes"][0]["release"] == "4.1.0"
        assert settings["control_version"] == versions["aiflow"]
        assert settings["configured_account_count"] == len(ctx.repo().accounts)
        assert machine["name"] == "node1" and machine["status"] == "online" and machine["slots_used"] == 2
        assert account["provider"] == "claude" and account["status"] == "idle"
        assert len(incidents) == 100
        assert all(item["kind"] != "old" for item in incidents)
        assert "inventory.json" in bundle.namelist()
    assert inventory["incident_count"] == 100
    assert inventory["scan"] == "passed"
    assert all(item["bytes"] <= support_bundle.MAX_LOG_BYTES for item in inventory["files"]
               if item["name"].startswith("logs/"))


def test_bundle_omits_untrusted_logger_name_from_timestamped_log(ctx):
    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("jane.doe", logging.INFO, __file__, 1, "hello", (), None))

    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=logs)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        log = bundle.read("logs/control-service.log").decode("utf-8")

    assert "jane.doe" not in log
    assert "INFO [REDACTED_FREEFORM]" in log
    assert inventory["scan"] == "passed"
    assert not support_bundle.file_findings("logs/control-service.log", log)


def test_bundle_does_not_trust_separator_or_arbitrary_log_field_names(ctx):
    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord(
        "aiflow4.test", logging.INFO, __file__, 1,
        "ordinary message\n--- jane.doe ---\n--- control-log-1 ---\nrequest jane.doe=1", (), None,
    ))

    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=logs)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        log = bundle.read("logs/control-service.log").decode("utf-8")

    assert "jane.doe" not in log
    assert "--- jane.doe ---" not in log
    assert "--- control-log-1 ---" not in log
    assert "jane.doe=1" not in log
    assert inventory["scan"] == "passed"
    assert not support_bundle.file_findings("logs/control-service.log", log)


def test_machine_names_survive_in_structured_files_while_log_names_stay_redacted(ctx):
    for name in ("wsl", "mac-mini", "wsl-1"):
        ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,slots_used,release_version)"
                   " VALUES (%s,'worker','online',2,0,'4.1.0')", (name,))
    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, "Jane Smith items=1", (), None))
    archive, _ = support_bundle.build_support_bundle(ctx, log_buffer=logs)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        machines = json.loads(bundle.read("machine-state.json"))
        versions = json.loads(bundle.read("versions.json"))
        log = bundle.read("logs/control-service.log").decode()
    assert {m["name"] for m in machines} >= {"wsl", "mac-mini", "wsl-1"}
    assert {n["name"] for n in versions["nodes"]} >= {"wsl", "mac-mini", "wsl-1"}
    assert "REDACTED_NAME" not in json.dumps(machines)
    assert "Jane Smith" not in log


def test_name_like_node_label_is_redacted_across_structured_bundle_files(ctx):
    from aiflow.v4.core.controlrepo import Node

    ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,slots_used,release_version)"
               " VALUES ('jane.doe','worker','online',2,0,'4.1.0')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
               " VALUES ('jane-doe','claude','jane.doe','middle',%s,'idle')", (["dev"],))
    ctx.repo().nodes["jane.doe"] = Node(name="jane.doe")

    raw_files = support_bundle._file_payloads(ctx, None, support_bundle.SupportLogBuffer())
    findings = {}
    for name, content in raw_files.items():
        file_scan = support_bundle.file_findings(name, content.decode("utf-8"))
        if file_scan:
            findings[name] = file_scan
    assert not findings, findings
    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        contents = {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}
        machine_states = json.loads(contents["machine-state.json"])
        versions = json.loads(contents["versions.json"])
        settings = json.loads(contents["settings.json"])
        account_states = json.loads(contents["account-state.json"])

    assert inventory["scan"] == "passed"
    assert all("jane.doe" not in content and "jane-doe" not in content
               for name, content in contents.items() if name.endswith(".json"))
    assert [row["name"] for row in machine_states if row["status"] == "online" and row["slots_total"] == 2
            and row["release"] == "4.1.0"] == ["[REDACTED_NAME]"]
    assert any(row["name"] == "[REDACTED_NAME]" for row in versions["nodes"])
    assert "[REDACTED_NAME]" in settings["configured_nodes"]
    assert any(row["node"] == "[REDACTED_NAME]" for row in account_states)
    assert "personal_data" in support_bundle.file_findings("machine-state.json", '[{"name":"jane.doe"}]')


def test_structured_files_still_redact_secrets_and_tokens():
    raw = json.dumps({"nodes": [{"name": "wsl", "release": "4.1.0"}],
                      "token": "abcd1234efgh5678", "note": "ghp_" + "a" * 20})
    safe = support_bundle._safe_json(json.loads(raw), "versions.json")
    assert '"name": "wsl"' in safe
    assert "abcd1234efgh5678" not in safe and "ghp_" not in safe


def test_incident_collection_excludes_incidents_older_than_seven_days(ctx):
    ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail,opened_at)"
               " VALUES ('recent','TESTWP-1','warning','Recent issue','Safe detail',now()-interval '6 days')")
    ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail,opened_at)"
               " VALUES ('stale','TESTWP-2','warning','Old issue','Safe detail',now()-interval '8 days')")

    archive, _ = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        incidents = json.loads(bundle.read("incidents.json"))
    assert [item["kind"] for item in incidents] == ["recent"]


def test_bundle_excludes_unscreenable_free_form_incident_text(ctx):
    incident_text = "jane doe called support yesterday"
    ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail)"
               " VALUES ('support','jane doe','warning',%s,%s)", (incident_text, incident_text))

    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        incidents = json.loads(bundle.read("incidents.json"))
        contents = "\n".join(bundle.read(name).decode("utf-8") for name in bundle.namelist())

    assert inventory["scan"] == "passed"
    assert incident_text not in contents
    assert incidents[0]["kind"] == "support"
    assert incidents[0]["subject"] == "[REDACTED_FREEFORM]"
    assert incidents[0]["title"] == "[REDACTED_FREEFORM]"
    assert incidents[0]["detail"] == "[REDACTED_FREEFORM]"


def test_bundle_scrubs_personal_data_and_credentials_before_archive_is_built(ctx):
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    email = "private.person@example.test"
    ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail)"
               " VALUES ('test','TESTWP-1','warning','Private incident',%s)",
               (f"owner=Alice; token={token}; contact {email}; phone +1 (415) 555-0134; Jane Doe",))

    archive, _ = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        contents = {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}
    joined = "\n".join(contents.values())
    assert token not in joined and email not in joined and "415) 555-0134" not in joined
    assert "Jane Doe" not in joined and "Alice" not in joined
    assert all(not support_bundle.file_findings(name, content) for name, content in contents.items())


def test_scanner_detects_secrets_tokens_keys_and_personal_data_and_blocks_offer(client, ctx, monkeypatch):
    raw = "token=ghp_abcdefghijklmnopqrstuvwxyz123456 owner@example.com +1 (415) 555-0134 192.0.2.44 John Smith"
    assert {"credential", "token", "personal_data"} <= set(support_bundle.sensitive_findings(raw))
    clean = support_bundle.sanitize_text(raw)
    assert not support_bundle.sensitive_findings(clean)
    for private in ("ghp_abcdefghijklmnopqrstuvwxyz123456", "owner@example.com", "John Smith", "192.0.2.44"):
        assert private not in clean
    nested = json.dumps({"detail": json.dumps({"api_key": "randomsecretvalue"})})
    clean_nested = support_bundle.sanitize_text(nested)
    assert json.loads(clean_nested)
    assert "randomsecretvalue" not in clean_nested

    sign_in_owner(client, ctx)
    monkeypatch.setattr(support_bundle, "sensitive_findings", lambda _text, **_kw: ["credential"])
    blocked = client.post("/v1/support/bundles", json={})
    assert blocked.status_code == 422
    assert "bundle_id" not in blocked.json()
    assert not list(ctx.leases.r.scan_iter(match=ctx.leases.p + "support_bundle:*"))


def test_google_api_key_in_log_query_is_redacted_before_bundle_offer(ctx):
    google_key = "AIza" + "A" * 35
    opaque_key = "opaque-query-key-value-123456"
    raw = (f"GET https://maps.example.test/lookup?key={google_key}&lang=en returned 200\n"
           f"GET https://internal.example.test/lookup?key={opaque_key}&lang=en returned 200")
    assert "token" in support_bundle.sensitive_findings(raw)

    clean = support_bundle.sanitize_text(raw)
    assert google_key not in clean and opaque_key not in clean
    assert not support_bundle.sensitive_findings(clean)

    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, raw, (), None))
    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=logs)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        log_text = bundle.read("logs/control-service.log").decode("utf-8")
    assert google_key not in log_text and opaque_key not in log_text
    assert inventory["scan"] == "passed"


@pytest.mark.parametrize("api_key", [
    "AIza" + "B" * 35,
    "sk_live_" + "C" * 24,
    "sk-ant-" + "D" * 24,
    "SG." + "E" * 20 + "." + "F" * 20,
])
def test_common_unlabeled_api_key_formats_are_redacted(api_key):
    assert "token" in support_bundle.sensitive_findings(api_key)
    clean = support_bundle.sanitize_text(api_key)
    assert api_key not in clean
    assert not support_bundle.sensitive_findings(clean)


@pytest.mark.parametrize("raw", [
    "ticket assigned to jane doe after a call",
    "customer date of birth: 1985-04-12",
])
def test_free_form_name_and_date_of_birth_are_redacted_and_block_if_unsanitized(ctx, monkeypatch, raw):
    assert "personal_data" in support_bundle.sensitive_findings(raw)
    clean = support_bundle.sanitize_text(raw)
    assert raw not in clean
    assert not support_bundle.sensitive_findings(clean)

    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, raw, (), None))
    monkeypatch.setattr(support_bundle, "structured_log_text", lambda text: text)
    monkeypatch.setattr(support_bundle, "sanitize_text", lambda text, **_kw: text)
    with pytest.raises(ValueError, match="sensitive content remains"):
        support_bundle.build_support_bundle(ctx, log_buffer=logs)


def test_secret_scanner_redacts_and_rejects_the_full_quoted_value():
    raw = 'password: "my secret phrase"'
    clean = support_bundle.sanitize_text(raw)

    assert "credential" in support_bundle.sensitive_findings(raw)
    assert clean == 'password: "[REDACTED]"'
    assert "my secret phrase" not in clean
    assert not support_bundle.sensitive_findings(clean)
    assert "credential" in support_bundle.sensitive_findings('password: "[REDACTED] secret phrase"')


def test_secret_scanner_redacts_session_and_cookie_credentials():
    for raw in ("session=abc123def456", "cookie=auth-cookie-value", "set-cookie: session=abc123"):
        clean = support_bundle.sanitize_text(raw)

        assert "credential" in support_bundle.sensitive_findings(raw)
        assert raw.split("=", 1)[-1].split(":", 1)[-1].strip() not in clean
        assert not support_bundle.sensitive_findings(clean)


def test_secret_scanner_redacts_full_unquoted_multiword_value():
    raw = "password=two words"
    clean = support_bundle.sanitize_text(raw)

    assert "credential" in support_bundle.sensitive_findings(raw)
    assert clean == "password=[REDACTED]"
    assert "two words" not in clean
    assert not support_bundle.sensitive_findings(clean)


def test_personal_data_scan_redacts_dotted_and_underscored_person_field_names():
    for raw in ("owner=jane.doe", "reporter=jane_doe"):
        clean = support_bundle.sanitize_text(raw)

        assert "personal_data" in support_bundle.sensitive_findings(raw)
        assert "jane" not in clean and "doe" not in clean
        assert not support_bundle.sensitive_findings(clean)


def test_personal_data_scan_redacts_single_name_in_person_field():
    raw = "owner=Alice"
    clean = support_bundle.sanitize_text(raw)

    assert "personal_data" in support_bundle.sensitive_findings(raw)
    assert clean == "owner=[REDACTED_NAME]"
    assert "Alice" not in clean
    assert not support_bundle.sensitive_findings(clean)


def test_lowercase_multiword_customer_name_is_redacted_and_unresolved_value_blocks_bundle(ctx, monkeypatch):
    raw = "customer name: jane doe"
    for labeled_name in (raw, "name: jane doe", "CUSTOMER_NAME=jane doe"):
        clean = support_bundle.sanitize_text(labeled_name)

        assert "personal_data" in support_bundle.sensitive_findings(labeled_name)
        assert "jane doe" not in clean
        assert not support_bundle.sensitive_findings(clean)
    assert support_bundle.sanitize_text(raw) == "customer name: [REDACTED_NAME]"

    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, raw, (), None))
    monkeypatch.setattr(support_bundle, "structured_log_text", lambda text: text)
    monkeypatch.setattr(support_bundle, "sanitize_text", lambda text, **_kw: text)
    with pytest.raises(ValueError, match="sensitive content remains"):
        support_bundle.build_support_bundle(ctx, log_buffer=logs)


def test_support_sanitizer_redacts_url_and_dsn_userinfo_credentials():
    cases = (
        ("postgresql://bundle_user:db-secret@db.internal/aiflow", "bundle_user", "db-secret"),
        ("https://api_user:api-secret@example.internal/status", "api_user", "api-secret"),
    )
    for raw, username, password in cases:
        clean = support_bundle.sanitize_text(raw)

        assert "credential" in support_bundle.sensitive_findings(raw)
        assert username not in clean and password not in clean
        assert not support_bundle.sensitive_findings(clean)


def test_support_sanitizer_redacts_unseparated_phone_and_spaced_ssn():
    for raw in ("phone=4155550134", "ssn=123 45 6789"):
        clean = support_bundle.sanitize_text(raw)

        assert "personal_data" in support_bundle.sensitive_findings(raw)
        assert raw.split("=", 1)[1] not in clean
        assert not support_bundle.sensitive_findings(clean)


def test_support_sanitizer_keeps_ordinary_log_text():
    raw = "2026-10-22 12:04:05 INFO aiflow4.control.api bundle build completed without errors"

    assert support_bundle.sanitize_text(raw) == raw
    assert not support_bundle.sensitive_findings(raw)


def test_secrets_in_the_middle_of_multiline_text_are_redacted_and_scanned():
    cases = [
        ("2026-10-23 12:00:00 INFO x password=hunter2\n2026-10-23 12:00:01 INFO y", "hunter2"),
        ("token=abc123 b\nc", "abc123"),
        ('API_KEY="abc"\nnext line', "abc"),
        ("start\r\nsession=zz9 plural\r\nend\r\n", "zz9"),
        ('{\n  "api_key": "k-77",\n  "state": "ok"\n}\n', "k-77"),
    ]
    for raw, secret in cases:
        assert "credential" in support_bundle.sensitive_findings(raw)
        clean = support_bundle.sanitize_text(raw)
        assert secret not in clean
        assert "[REDACTED]" in clean
        assert not support_bundle.sensitive_findings(clean)
    clean = support_bundle.sanitize_text("a password=hunter2\nnext line stays")
    assert clean.endswith("\nnext line stays")


def test_scan_gate_rejects_unredacted_secret_in_multiline_incident_detail(ctx):
    detail = "line one\npassword=hunter2\nline three"
    assert "credential" in support_bundle.sensitive_findings(detail)
    assert "hunter2" not in support_bundle.sanitize_text(detail)


def test_standard_deployment_routes_support_screen_to_control_api():
    compose = Path(__file__).resolve().parents[2] / "deploy" / "v4" / "control" / "compose.yaml"
    rule = next(line for line in compose.read_text(encoding="utf-8").splitlines()
                if "traefik.http.routers.api.rule:" in line)

    assert "PathPrefix(`/support`)" in rule


def test_owner_reviews_inventory_before_download_and_lead_cannot_build(client, ctx):
    sign_in_owner(client, ctx)
    created = client.post("/v1/support/bundles", json={})
    assert created.status_code == 200
    data = created.json()
    assert data["inventory"]["scan"] == "passed"
    assert {item["name"] for item in data["inventory"]["files"]} >= {
        "versions.json", "settings.json", "machine-state.json", "account-state.json", "incidents.json",
        "logs/control-service.log", "inventory.json"}
    assert "archive" not in data and "bytes" not in data
    downloaded = client.get(data["download_url"])
    assert downloaded.status_code == 200 and downloaded.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        assert json.loads(bundle.read("inventory.json"))["scan"] == "passed"

    from fastapi.testclient import TestClient
    from aiflow.v4.control.api import create_app

    other_replica = TestClient(create_app(ctx))
    sign_in_owner(other_replica, ctx)
    assert other_replica.get(data["download_url"]).status_code == 200

    sign_in_owner(other_replica, ctx, subject="second-admin@example.test")
    assert other_replica.get(data["download_url"]).status_code == 403

    sign_in_owner(client, ctx, "lead")
    assert client.post("/v1/support/bundles", json={}).status_code == 403


def test_run_result_keeps_raw_logs_out_of_the_runs_table_and_stores_a_sanitized_copy_with_ttl(client, ctx):
    from conftest import add_ticket
    from test_api import join

    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-240")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-240", "account": "claude-01"}, headers=cc).json()
    raw = "started attempt=3\npassword=hunter2hunter2\ncontact alice@example.test\n"
    res = client.post(f"/v1/runs/{r['run_id']}/result", headers=cc,
                      json={"fence": r["fence"], "outcome": "done",
                            "details": {"support_log_text": raw, "support_log_truncated": False, "kept": 1}})
    assert res.status_code == 200, res.text
    details = ctx.db.one("SELECT details FROM runs WHERE id=%s", (r["run_id"],))["details"]
    assert details.get("kept") == 1 and "support_log_text" not in details and "support_log_truncated" not in details
    stored = support_bundle.load_run_log(ctx, r["run_id"])
    assert "attempt=3" in stored and "hunter2" not in stored and "alice@example.test" not in stored
    assert 0 < ctx.leases.r.ttl(support_bundle._run_log_key(ctx, r["run_id"])) <= support_bundle.RUN_LOG_TTL_S

def test_persistent_control_logs_are_filtered_to_the_last_24_hours(tmp_path, ctx):
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    recent = now.strftime("%Y-%m-%d %H:%M:%S")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "control-api-a.log").write_text(f"{stale} INFO aiflow4.api old entry n=1\n{recent} INFO aiflow4.api recent entry n=2\n",
                                                 encoding="utf-8")
    ctx.settings.support_log_dir = str(log_dir)
    archive, _ = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        log_text = bundle.read("logs/control-service.log").decode()
    assert "n=2" in log_text
    assert "n=1" not in log_text
    assert "--- control-log-1 ---" in log_text


def test_control_loops_installs_support_log_capture(tmp_path, ctx, monkeypatch):
    from aiflow.v4.control import loops

    ctx.settings.support_log_dir = str(tmp_path / "logs")
    ctx.settings.o2_source = "control-loops"
    monkeypatch.setattr(ctx.events, "start", lambda: None)
    installed = []
    install_capture = support_bundle.control_log_buffer

    def tracked_capture(log_dir, source):
        installed.append((log_dir, source))
        return install_capture(log_dir, source)

    monkeypatch.setattr(support_bundle, "control_log_buffer", tracked_capture)
    stop = threading.Event()
    stop.set()
    loops.run_loops(ctx, stop)

    message = f"loop capture regression {uuid.uuid4()}"
    logging.getLogger("aiflow4.loops").warning(message)
    assert installed == [(str(tmp_path / "logs"), "control-loops")]
    assert message in install_capture().text()


def test_ticket_bundle_includes_only_selected_ticket_runs_from_last_24_hours_and_caps_each_log(ctx):
    client_app = __import__("aiflow.v4.control.api", fromlist=["create_app"]).create_app(ctx)
    client_app.state.sync_accounts()
    from fastapi.testclient import TestClient

    client = TestClient(client_app)
    sign_in_owner(client, ctx)
    ctx.db.run("INSERT INTO tickets (key,project,status,type,summary) VALUES ('TESTWP-235','TESTWP','To Do','Bug',%s)",
               ("Support regression",))
    run_id = add_run(ctx, "TESTWP-235", "items=1\n" * (support_bundle.MAX_LOG_BYTES // 7))
    add_run(ctx, "TESTWP-235", "too old", age_hours=25)
    add_run(ctx, "TESTWP-236", "other ticket")

    created = client.post("/v1/support/bundles", json={"ticket_key": "TESTWP-235"})
    assert created.status_code == 200, created.text
    assert created.json()["inventory"]["ticket_run_log_count"] == 1
    downloaded = client.get(created.json()["download_url"])
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        run_logs = [name for name in bundle.namelist() if name.startswith("logs/ticket-runs/")]
        assert len(run_logs) == 1 and run_id in run_logs[0]
        assert len(bundle.read(run_logs[0])) <= support_bundle.MAX_LOG_BYTES
        assert json.loads(bundle.read("ticket.json"))["ticket_key"] == "TESTWP-235"


def test_worker_attaches_only_a_capped_log_within_the_run_log_directory(tmp_path):
    root = tmp_path / "aiflow"
    log_dir = root / "logs" / "runs"
    log_dir.mkdir(parents=True)
    path = log_dir / "TESTWP-1-run.jsonl"
    path.write_text("z" * (support_bundle.MAX_LOG_BYTES + 30), encoding="utf-8")
    result = {"details": {"log": str(path)}}

    attach_support_log(result, str(root))
    assert result["details"]["support_log_truncated"] is True
    assert len(result["details"]["support_log_text"].encode("utf-8")) == support_bundle.MAX_LOG_BYTES

    path.write_text("€" * (support_bundle.MAX_LOG_BYTES // 3 + 1), encoding="utf-8")
    unicode_result = {"details": {"log": str(path)}}
    attach_support_log(unicode_result, str(root))
    unicode_log = unicode_result["details"]["support_log_text"]
    assert unicode_result["details"]["support_log_truncated"] is True
    assert len(unicode_log.encode("utf-8")) <= support_bundle.MAX_LOG_BYTES
    assert unicode_log.encode("utf-8").decode("utf-8") == unicode_log

    outside = tmp_path / "private.log"
    outside.write_text("do not collect", encoding="utf-8")
    unsafe = {"details": {"log": str(outside)}}
    attach_support_log(unsafe, str(root))
    assert "support_log_text" not in unsafe["details"]


def test_log_cap_drops_partial_utf8_codepoints_without_exceeding_byte_limit():
    capped, truncated = support_bundle.cap_log_bytes("€" * 3, max_bytes=5)

    assert truncated is True
    assert capped == "€"
    assert len(capped.encode("utf-8")) <= 5


JANE = "jane doe called support yesterday"


def test_free_form_text_is_absent_from_every_file_of_ticket_bundle(ctx):
    ctx.db.run("INSERT INTO tickets (key,project,status,type,summary) VALUES ('TESTWP-300','TESTWP','To Do','Bug',%s)",
               (f"fix login for jane doe; {JANE}",))
    ctx.db.run("INSERT INTO incidents (kind,subject,severity,title,detail) VALUES ('test',%s,'warning',%s,%s)",
               (JANE, JANE, JANE))
    add_run(ctx, "TESTWP-300", f"2026-10-23 10:00:00 INFO worker {JANE} status=200\n{JANE}\npwd=hunter2hunter2\n")
    logs = support_bundle.SupportLogBuffer()
    logs.emit(logging.LogRecord("aiflow4.test", logging.INFO, __file__, 1, JANE + " code=7", (), None))

    archive, inventory = support_bundle.build_support_bundle(ctx, "TESTWP-300", log_buffer=logs)
    assert inventory["scan"] == "passed" and inventory["ticket_run_log_count"] == 1
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert any(name.startswith("logs/ticket-runs/") for name in bundle.namelist())
        for name in bundle.namelist():
            text = bundle.read(name).decode("utf-8").lower()
            assert "jane" not in text and "doe" not in text and "yesterday" not in text, name
            assert "hunter2" not in text, name
        assert json.loads(bundle.read("ticket.json"))["summary"] == support_bundle.FREEFORM
        assert "code=7" in bundle.read("logs/control-service.log").decode()


@pytest.mark.parametrize("raw", ["pwd=hunter2hunter2", "pass: hunter2hunter2", "PASSPHRASE=hunter2hunter2",
                                 'db_pwd="hunter2hunter2"'])
def test_pwd_and_pass_style_fields_are_redacted_and_flagged(raw):
    assert "credential" in support_bundle.sensitive_findings(raw)
    clean = support_bundle.sanitize_text(raw)
    assert "hunter2" not in clean and not support_bundle.sensitive_findings(clean)


def test_lowercase_name_shaped_labels_are_not_offered_in_a_bundle(ctx):
    ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,slots_used,release_version)"
               " VALUES ('john-smith','worker','online',1,0,'4.1.0'), ('mac-mini','worker','online',1,0,'4.1.0')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
               " VALUES ('jane-doe','claude','mac-mini','middle',%s,'idle'),"
               " ('claude-01','claude','mac-mini','middle',%s,'idle')", (["dev"], ["dev"]))
    assert support_bundle.file_findings("account-state.json", '[{"id":"jane-doe"}]') == ["personal_data"]
    archive, _ = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        everything = " ".join(bundle.read(n).decode() for n in bundle.namelist())
    assert "jane-doe" not in everything and "john-smith" not in everything
    assert "mac-mini" in everything and "claude-01" in everything


def test_phone_shaped_labels_are_not_offered_in_a_bundle(ctx):
    ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,slots_used,release_version)"
               " VALUES ('555-123-4567','worker','online',1,0,'4.1.0'), ('mac-mini','worker','online',1,0,'4.1.0')")
    ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
               " VALUES ('555-123-4567','claude','mac-mini','middle',%s,'idle'),"
               " ('claude-01','claude','mac-mini','middle',%s,'idle')", (["dev"], ["dev"]))
    assert support_bundle.file_findings("account-state.json", '[{"id":"555-123-4567"}]') == ["personal_data"]
    archive, inventory = support_bundle.build_support_bundle(ctx, log_buffer=support_bundle.SupportLogBuffer())
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        everything = " ".join(bundle.read(n).decode() for n in bundle.namelist())
    assert inventory["scan"] == "passed"
    assert "555-123-4567" not in everything
    assert "mac-mini" in everything and "claude-01" in everything
