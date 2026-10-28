from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from pathlib import Path

import pytest
from aiflow.v4.control import login, quota_refresh
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.db import J
from aiflow.v4.worker.quota_cli import _ready_for_slash_command, parse_quota_text, read_quota
from conftest import add_ticket, join_node


def node_headers(client, name="cc"):
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": "Bearer " + joined.json()["node_token"]}


def test_refresh_interval_defaults_to_fifteen_minutes_and_can_be_changed(ctx):
    assert quota_refresh.get_settings(ctx.db)["interval_minutes"] == 15
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('first-refresh','claude','cc')"
               " ON CONFLICT (id) DO NOTHING")
    ctx.db.run("INSERT INTO quota_refresh_jobs (account_id,next_due_at)"
               " VALUES ('first-refresh',now()+interval '1 hour') ON CONFLICT (account_id) DO NOTHING")
    updated = quota_refresh.set_settings(ctx.db, 23, "admin@example.test")
    assert updated["interval_minutes"] == 23
    assert updated["updated_by"] == "admin@example.test"
    job = ctx.db.one("SELECT next_due_at,last_attempt_at FROM quota_refresh_jobs WHERE account_id='first-refresh'")
    assert job["last_attempt_at"] is None and job["next_due_at"] <= datetime.now(timezone.utc) + timedelta(seconds=1)
    attempted_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    ctx.db.run("UPDATE quota_refresh_jobs SET last_attempt_at=%s,next_due_at=now()+interval '1 hour'"
               " WHERE account_id='first-refresh'", (attempted_at,))
    quota_refresh.set_settings(ctx.db, 7, "admin@example.test")
    job = ctx.db.one("SELECT next_due_at FROM quota_refresh_jobs WHERE account_id='first-refresh'")
    assert abs((job["next_due_at"] - (attempted_at + timedelta(minutes=7))).total_seconds()) < 1
    try:
        quota_refresh.set_settings(ctx.db, 0, "admin@example.test")
    except ValueError as exc:
        assert "between 1 and 1440" in str(exc)
    else:
        raise AssertionError("invalid refresh interval was accepted")


def test_unproven_legacy_quota_is_not_used_as_a_fresh_provider_report(ctx):
    from aiflow.v4.control.quota import account_assessment

    quota = {"windows": {"weekly": {"utilization": .95}}}
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='',quota_observed_at=NULL WHERE id='claude-01'",
               (J(quota),))
    ctx.db.run("INSERT INTO account_quota_samples (account_id,provider,quota,source)"
               " VALUES ('claude-01','claude',%s,'legacy')", (J(quota),))
    assessment = account_assessment(ctx.db, "claude-01", "claude", quota)
    assert assessment["is_estimate"] is True
    assert [window["name"] for window in assessment["windows"]] == ["estimated_week"]


def test_account_refresh_lease_is_exclusive_and_records_cli_snapshot(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('claude-01','claude','cc')"
               " ON CONFLICT (id) DO NOTHING")
    first = quota_refresh.claim(ctx.db, "claude-01", "cc")
    assert first and first["lease_token"]
    assert quota_refresh.claim(ctx.db, "claude-01", "node1") is None

    quota = {"provider": "claude", "windows": {"five_hour": {"utilization": .27}}}
    assert quota_refresh.finish(ctx.db, "claude-01", first["lease_token"], ok=True, quota=quota,
                                source="cli", model="")
    account = ctx.db.one("SELECT quota,quota_source,quota_observed_at,quota_forecast FROM accounts"
                         " WHERE id='claude-01'")
    assert account["quota"] == quota and account["quota_source"] == "cli" and account["quota_observed_at"]
    assert account["quota_forecast"]["source"] == "reported"
    assert account["quota_forecast"]["windows"][0]["used_pct"] == 27.0
    sample = ctx.db.one("SELECT source,observed_at,sampled_at,tokens_in,cost_usd FROM account_quota_samples"
                        " WHERE account_id='claude-01'")
    assert sample["source"] == "cli" and sample["observed_at"] == sample["sampled_at"]
    assert sample["tokens_in"] == 0 and float(sample["cost_usd"]) == 0
    assert quota_refresh.claim(ctx.db, "claude-01", "cc") is None


def test_concurrent_nodes_can_claim_only_one_account_quota_refresh(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('refresh-race','claude','cc')")
    barrier = Barrier(3)

    def request_claim(node):
        barrier.wait(timeout=5)
        return quota_refresh.claim(ctx.db, "refresh-race", node)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request_claim, node) for node in ("cc", "node1")]
        barrier.wait(timeout=5)
        claims = [future.result(timeout=10) for future in futures]
    claimed = [claim for claim in claims if claim]
    assert len(claimed) == 1
    old_token = claimed[0]["lease_token"]
    ctx.db.run("UPDATE quota_refresh_jobs SET lease_expires_at=now()-interval '1 second'"
               " WHERE account_id='refresh-race'")
    retried = quota_refresh.claim(ctx.db, "refresh-race", "node1")
    assert retried and retried["lease_token"] != old_token


def test_refresh_does_not_clear_a_task_quota_cooldown(client, ctx):
    headers = node_headers(client)
    ctx.db.run("UPDATE accounts SET status='cooldown',cooldown_until=now()+interval '10 minutes',"
               "cooldown_since=now() WHERE id='claude-01'")
    ctx.leases.cooldown("claude-01", 600, "quota")
    claim = client.post("/v1/accounts/claude-01/quota-refresh/claim", headers=headers).json()["job"]
    quota = {"provider": "claude", "windows": {"five_hour": {"utilization": .99}}}
    result = client.post("/v1/accounts/claude-01/quota-refresh/result", headers=headers, json={
        "lease_token": claim["lease_token"], "ok": True, "quota": quota})
    assert result.status_code == 200
    account = ctx.db.one("SELECT status,cooldown_until FROM accounts WHERE id='claude-01'")
    assert account["status"] == "cooldown" and account["cooldown_until"] > datetime.now(timezone.utc)
    assert ctx.leases.cooling("claude-01")


def test_refresh_waits_for_managed_signin_then_satisfies_its_check_only_afterwards(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('signin-refresh','codex','cc','signed_out')")
    ctx.db.run("INSERT INTO managed_ai_accounts (id,provider,node,sign_in_status,sign_in_started_at)"
               " VALUES ('signin-refresh','codex','cc','waiting',now())")
    assert quota_refresh.claim(ctx.db, "signin-refresh", "cc") is None
    ctx.db.run("UPDATE managed_ai_accounts SET sign_in_status='completed',"
               "sign_in_started_at=now()-interval '1 second',updated_at=now() WHERE id='signin-refresh'")
    claim = quota_refresh.claim(ctx.db, "signin-refresh", "cc")
    assert claim
    observed_at = claim["claimed_at"] + timedelta(milliseconds=10)
    quota = {"provider": "codex", "windows": {"five_hour": {"utilization": .25}}}
    assert quota_refresh.finish(ctx.db, "signin-refresh", claim["lease_token"], ok=True, quota=quota,
                                observed_at=observed_at)
    account = ctx.db.one("SELECT status,last_probe_at FROM accounts WHERE id='signin-refresh'")
    assert account["status"] == "idle" and account["last_probe_at"] >= claim["claimed_at"]


def test_refresh_started_before_signin_cannot_satisfy_it_and_is_retried(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('signin-race','codex','cc','signed_out')")
    ctx.db.run("INSERT INTO managed_ai_accounts (id,provider,node,sign_in_status)"
               " VALUES ('signin-race','codex','cc','idle')")
    old_claim = quota_refresh.claim(ctx.db, "signin-race", "cc")
    assert old_claim
    ctx.db.run("UPDATE managed_ai_accounts SET sign_in_status='completed',sign_in_started_at=clock_timestamp(),"
               "updated_at=now()"
               " WHERE id='signin-race'")
    quota = {"provider": "codex", "windows": {"five_hour": {"utilization": .4}}}
    assert quota_refresh.finish(ctx.db, "signin-race", old_claim["lease_token"], ok=True, quota=quota)
    account = ctx.db.one("SELECT status,last_probe_at FROM accounts WHERE id='signin-race'")
    assert account["status"] == "signed_out" and account["last_probe_at"] is None
    assert ctx.db.one("SELECT next_due_at<=now()+interval '1 second' AS due FROM quota_refresh_jobs"
                      " WHERE account_id='signin-race'")["due"]

    current_claim = quota_refresh.claim(ctx.db, "signin-race", "cc")
    assert current_claim and current_claim["claimed_at"] >= ctx.db.one(
        "SELECT sign_in_started_at FROM managed_ai_accounts WHERE id='signin-race'")["sign_in_started_at"]
    assert quota_refresh.finish(ctx.db, "signin-race", current_claim["lease_token"], ok=True, quota=quota)
    account = ctx.db.one("SELECT status,last_probe_at FROM accounts WHERE id='signin-race'")
    assert account["status"] == "idle" and account["last_probe_at"]


def test_deleting_account_removes_its_refresh_job(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('claude-removed','claude','cc')")
    assert quota_refresh.claim(ctx.db, "claude-removed", "cc")
    ctx.db.run("DELETE FROM accounts WHERE id='claude-removed'")
    assert ctx.db.one("SELECT account_id FROM quota_refresh_jobs WHERE account_id='claude-removed'") is None


def test_older_refresh_cannot_replace_a_newer_run_snapshot(ctx):
    ctx.db.run("INSERT INTO accounts (id,provider,node) VALUES ('claude-01','claude','cc')"
               " ON CONFLICT (id) DO NOTHING")
    claim = quota_refresh.claim(ctx.db, "claude-01", "cc")
    assert claim
    newer = claim["claimed_at"] + timedelta(seconds=2)
    run_quota = {"provider": "claude", "windows": {"five_hour": {"utilization": .61}}}
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='run',quota_observed_at=%s WHERE id='claude-01'",
               (J(run_quota), newer))
    old_quota = {"provider": "claude", "windows": {"five_hour": {"utilization": .58}}}
    assert quota_refresh.finish(ctx.db, "claude-01", claim["lease_token"], ok=True, quota=old_quota,
                                observed_at=claim["claimed_at"] + timedelta(seconds=1))
    account = ctx.db.one("SELECT quota,quota_source,quota_observed_at FROM accounts WHERE id='claude-01'")
    assert account["quota"] == run_quota and account["quota_source"] == "run"
    assert account["quota_observed_at"] == newer
    sample = ctx.db.one("SELECT source,observed_at FROM account_quota_samples WHERE account_id='claude-01'")
    assert sample["source"] == "cli" and sample["observed_at"] < newer


def test_refresh_api_uses_node_auth_and_enforces_account_lease(client, ctx):
    headers = node_headers(client)
    path = "/v1/accounts/claude-01/quota-refresh/claim"
    claim = client.post(path, headers=headers)
    assert claim.status_code == 200 and claim.json()["job"]["provider"] == "claude"
    assert client.post(path, headers=headers).json() == {"job": None}

    job = claim.json()["job"]
    quota = {"provider": "claude", "windows": {"weekly": {"utilization": .4}}}
    result = client.post("/v1/accounts/claude-01/quota-refresh/result", headers=headers, json={
        "lease_token": job["lease_token"], "ok": True, "quota": quota})
    assert result.status_code == 200 and result.json() == {"recorded": True}
    assert client.post("/v1/accounts/claude-01/quota-refresh/result", headers=headers, json={
        "lease_token": job["lease_token"], "ok": True, "quota": quota}).status_code == 409
    assert client.post("/v1/accounts/codex-01/quota-refresh/claim", headers=headers).status_code == 403


def test_run_without_quota_does_not_create_a_fresh_sample(client, ctx):
    from aiflow.v4.control.db import J

    headers = node_headers(client)
    heartbeat = client.post("/v1/nodes/cc/heartbeat", headers=headers,
                            json={"slots_total": 2, "workers": [{"account": "claude-01", "state": "idle"}]})
    assert heartbeat.status_code == 200
    old = {"provider": "claude", "windows": {"weekly": {"utilization": .35}}}
    ctx.db.run("UPDATE accounts SET quota=%s,quota_source='',quota_observed_at=NULL,quota_forecast='{}'::jsonb"
               " WHERE id='claude-01'", (J(old),))
    add_ticket(ctx.db, "TESTWP-QREF-1")
    claimed = client.post("/v1/work/claim", headers=headers,
                          json={"ticket": "TESTWP-QREF-1", "account": "claude-01"}).json()
    assert claimed["claimed"]
    response = client.post(f"/v1/runs/{claimed['run_id']}/result", headers=headers,
                           json={"fence": claimed["fence"], "outcome": "done",
                                 "tokens_in": 120, "tokens_out": 30})
    assert response.status_code == 200
    assert ctx.db.one("SELECT count(*) AS n FROM account_quota_samples WHERE account_id='claude-01'")["n"] == 0
    account = ctx.db.one("SELECT quota,quota_source,quota_observed_at,quota_forecast FROM accounts"
                         " WHERE id='claude-01'")
    assert account["quota"] == old and account["quota_source"] == "" and account["quota_observed_at"] is None
    assert account["quota_forecast"]["source"] == "estimate"


def test_settings_api_is_console_managed_and_returns_per_account_status(client, ctx):
    ctx.settings.console_session_secret = "quota-refresh-test-session-secret-with-32-bytes"
    login.seed_user(ctx.db, "quota-admin@example.test", "administrator", "Quota Administrator")
    client.cookies.set(COOKIE_NAME, issue_session("quota-admin@example.test", "administrator",
                                                  2_000_000_000, ctx.settings.console_session_secret))
    response = client.get("/v1/quota-refresh/settings")
    assert response.status_code == 200
    assert response.json()["settings"]["interval_minutes"] == 15
    assert response.json()["jobs"] == []
    saved = client.put("/v1/quota-refresh/settings", json={"interval_minutes": 7})
    assert saved.status_code == 200 and saved.json()["settings"]["interval_minutes"] == 7
    assert client.put("/v1/quota-refresh/settings", json={"interval_minutes": 1441}).status_code == 422
    client.cookies.clear()
    login.seed_user(ctx.db, "quota-reviewer@example.test", "reviewer", "Quota Reviewer")
    client.cookies.set(COOKIE_NAME, issue_session("quota-reviewer@example.test", "reviewer",
                                                  2_000_000_000, ctx.settings.console_session_secret))
    assert client.get("/v1/quota-refresh/settings").status_code == 403


def test_quota_text_parser_uses_only_explicitly_labeled_windows():
    parsed = parse_quota_text("codex", "Rate limits: 5h 42% used · weekly 81% remaining")
    assert parsed == {"provider": "codex", "windows": {
        "five_hour": {"utilization": .42}, "weekly": {"utilization": .19}}}
    assert parse_quota_text("codex", "Context window: 42% used") == {}
    claude = parse_quota_text("claude", "Current session 55% used; current week 73% used")
    assert claude["windows"] == {"five_hour": {"utilization": .55}, "weekly": {"utilization": .73}}


def test_codex_session_context_percentage_does_not_mask_five_hour_limit():
    parsed = parse_quota_text("codex", "Session: abc Context window: 95% left 5h limit: 42% used")

    assert parsed == {"provider": "codex", "windows": {"five_hour": {"utilization": .42}}}


def test_quota_text_parser_normalizes_relative_and_absolute_reset_times():
    observed_at = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    parsed = parse_quota_text(
        "codex", "5h 42% used; resets in 3h 15m. weekly 81% remaining; resets at 2026-10-29T09:30:00Z",
        observed_at)
    assert parsed["windows"] == {
        "five_hour": {"utilization": .42, "resets_at": "2026-10-27T15:15:00+00:00"},
        "weekly": {"utilization": .19, "resets_at": "2026-10-29T09:30:00+00:00"}}


def test_quota_cli_readiness_requires_prompt_at_terminal_tail():
    diagnostic = "worker.py: failed near › during startup\nCould not initialize terminal"
    trust_dialog = ("› Ask Codex to do anything\nFolder access\nTrust this folder?\n"
                    "› 1. Trust and continue\n2. Quit")
    assert not _ready_for_slash_command("codex", diagnostic.encode())
    assert not _ready_for_slash_command("codex", trust_dialog.encode())
    assert not _ready_for_slash_command("claude", "worker.py: error ❯\nstartup failed".encode())
    assert _ready_for_slash_command("codex", "startup complete\n› ".encode())
    assert _ready_for_slash_command("codex", "\x1b[21;1H› Ask Codex to do anything\n? for shortcuts".encode())
    assert _ready_for_slash_command("codex", "›\n  Ask Codex to do anything\n? for shortcuts".encode())
    assert _ready_for_slash_command("claude", "startup complete\n❯ ".encode())


def test_quota_cli_sends_only_fixed_read_only_command_after_prompt(tmp_path, monkeypatch):
    from aiflow.v4.worker import quota_cli

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "command.log"
    fake_cli = bin_dir / "codex"
    fake_cli.write_text("#!/bin/sh\nprintf '› '\nIFS= read -r command\n"
                        f"printf '%s\\n' \"$command\" > {shlex.quote(str(command_log))}\n"
                        "printf 'Rate limits: 5h 42%% used; weekly 81%% remaining\\n'\n"
                        "IFS= read -r exit_command\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    def deny_group_signal(_pid, _signal):
        raise PermissionError

    monkeypatch.setattr(quota_cli.os, "killpg", deny_group_signal)

    quota, error, observed_at = read_quota("codex", str(tmp_path), run_as=[], timeout_s=2,
                                           start_delay_s=.01, response_wait_s=.05)

    assert error == ""
    assert observed_at
    assert quota == {"provider": "codex", "windows": {
        "five_hour": {"utilization": .42}, "weekly": {"utilization": .19}}}
    assert command_log.read_text().strip() == "/status"


def test_quota_cli_cleanup_does_not_block_on_an_unreaped_child(monkeypatch):
    import threading
    import time

    from aiflow.v4.worker import quota_cli

    waits = []
    reapers = []

    def deny_group_signal(_pid, _signal):
        raise PermissionError

    def report_running(_pid, options):
        waits.append(options)
        return 0, 0

    monkeypatch.setattr(quota_cli.os, "killpg", deny_group_signal)
    monkeypatch.setattr(quota_cli.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(quota_cli.os, "waitpid", report_running)
    monkeypatch.setattr(threading.Thread, "start", lambda _thread: reapers.append(True))

    started = time.monotonic()
    quota_cli._stop_process_group(123, grace_s=0.01)

    assert time.monotonic() - started < 0.5
    assert waits and all(options == quota_cli.os.WNOHANG for options in waits)
    assert reapers == [True]


def test_quota_cli_does_not_send_command_without_interactive_prompt(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_cli = bin_dir / "codex"
    fake_cli.write_text("#!/bin/sh\nprintf 'Please sign in to continue\\n'\nsleep 2\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    quota, error, observed_at = read_quota("codex", str(tmp_path), run_as=[], timeout_s=.15,
                                           start_delay_s=.01, response_wait_s=.01)

    assert quota == {} and observed_at is None
    assert "no quota command was sent" in error


def test_worker_quota_cli_uses_an_isolated_helper_process(tmp_path, monkeypatch):
    from aiflow.v4.worker.quota_cli import read_quota_isolated

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "isolated-command.log"
    fake_cli = bin_dir / "codex"
    fake_cli.write_text("#!/bin/sh\nprintf '› '\nIFS= read -r command\n"
                        f"printf '%s\\n' \"$command\" > {shlex.quote(str(command_log))}\n"
                        "printf 'Rate limits: 5h 31%% used\\n'\nIFS= read -r exit_command\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    quota, error, observed_at = read_quota_isolated("codex", str(tmp_path), run_as=[], timeout_s=2,
                                                    start_delay_s=.01, response_wait_s=.05)

    assert error == ""
    assert observed_at
    assert quota == {"provider": "codex", "windows": {"five_hour": {"utilization": .31}}}
    assert command_log.read_text().strip() == "/status"


def test_settings_view_and_documentation_expose_the_refresh_controls():
    root = Path(__file__).resolve().parents[2]
    console = (root / "aiflow/v4/control/ui/console.html").read_text()
    settings = (root / "aiflow/v4/control/ui/quota_refresh_settings.js").read_text()
    docs = (root / "docs/v4/quota-refresh.md").read_text()
    readme = (root / "deploy/v4/README.md").read_text()
    assert 'src="/console-assets/quota-refresh-settings.js"' in console
    assert "Account quota refresh" in settings
    assert "data-quota-refresh-interval" in settings and "/v1/quota-refresh/settings" in settings
    assert "no quota snapshot, it does not" in " ".join(docs.split())
    assert "Scheduled account quota refresh" in readme


def test_quota_refresh_settings_asset_loads_status_and_saves_interval():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the quota settings panel")
    root = Path(__file__).resolve().parents[2]
    panel = (root / "aiflow/v4/control/ui/quota_refresh_settings.js").read_text()
    driver = r"""
var clicks = {}, calls = [], renderCount = 0, field = {value: '9'};
global.window = {};
global.document = {
  addEventListener: function (name, callback) { clicks[name] = callback; },
  querySelector: function () { return field; }
};
global.fetch = function (url, options) {
  options = options || {};
  calls.push({url: url, method: options.method || 'GET', body: options.body || ''});
  if (options.method === 'PUT') return Promise.resolve({settings: {interval_minutes: 9}, jobs: []});
  return Promise.resolve({settings: {interval_minutes: 19}, jobs: [{account_id: 'claude-01', provider: 'claude',
    last_attempt_at: '2026-10-27T10:00:00Z', last_success_at: '2026-10-27T10:00:01Z',
    next_due_at: '2026-10-27T10:15:00Z', last_source: 'cli', last_error: ''}]});
};
var state = {};
function responseError(response) { return Promise.resolve(response); }
function render() { renderCount += 1; }
function esc(value) { return String(value); }
function when(value) { return value; }
(async function () {
  eval(PANEL_SOURCE);
  var initial = window.AIFlowQuotaRefresh.panel(state, esc, when, render, responseError);
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  var loaded = window.AIFlowQuotaRefresh.panel(state, esc, when, render, responseError);
  var prevented = false, stopped = false;
  clicks.click({target: {closest: function () { return {}; }}, preventDefault: function () { prevented = true; },
    stopImmediatePropagation: function () { stopped = true; }});
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  console.log(JSON.stringify({initial: initial, loaded: loaded, calls: calls, notice: state.notice,
    renderCount: renderCount, prevented: prevented, stopped: stopped}));
})().catch(function (error) { console.error(error.stack); process.exitCode = 1; });
""".replace("PANEL_SOURCE", json.dumps(panel))
    result = subprocess.run([node, "-e", driver], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert 'value="15"' in output["initial"]
    assert 'value="19"' in output["loaded"] and "claude-01" in output["loaded"]
    assert output["calls"][0]["url"] == "/v1/quota-refresh/settings"
    assert output["calls"][1]["method"] == "PUT"
    assert json.loads(output["calls"][1]["body"]) == {"interval_minutes": 9}
    assert output["notice"] == "Quota refresh interval saved."
    assert output["prevented"] and output["stopped"] and output["renderCount"] >= 2
