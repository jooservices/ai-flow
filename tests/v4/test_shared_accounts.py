"""Shared AI accounts: one account enabled on several machines with one total session limit (JAIF-246)."""
from __future__ import annotations

import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event

import pytest

from conftest import add_ticket, join_node

from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.state import snapshot

SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("owner@example.test", role, 2_000_000_000, SESSION_SECRET))


def join(client, name):
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def beat(client, node, headers):
    ctx = client.app.state.ctx
    row = ctx.db.one("SELECT readiness FROM nodes WHERE name=%s", (node,)) or {}
    readiness = row.get("readiness") or {}
    credential_checks = dict(readiness.get("credential_statuses") or {})
    for account in ctx.repo().accounts_on(node):
        machine = ctx.db.one("SELECT status FROM account_machines WHERE account_id=%s AND node=%s",
                             (account.id, node))
        if machine:
            signed_in = machine.get("status") == "signed_in"
            credential_checks[account.id] = {"present": signed_in, "valid": signed_in}
    # Accounts created in Command Center are overlaid at runtime and may not be in the configured repo snapshot.
    # Their per-machine sign-in state is still part of the node's heartbeat readiness report.
    for machine in ctx.db.all("SELECT account_id,status FROM account_machines WHERE node=%s", (node,)):
        signed_in = machine["status"] == "signed_in"
        credential_checks[machine["account_id"]] = {"present": signed_in, "valid": signed_in}
    r = client.post(f"/v1/nodes/{node}/heartbeat",
                    json={"slots_total": 4, "credential_checks": credential_checks}, headers=headers)
    assert r.status_code == 200
    thread_name = f"aiflow4-heartbeat-readiness-{node}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        threads = [thread for thread in threading.enumerate() if thread.name == thread_name]
        if not threads:
            break
        for thread in threads:
            thread.join(timeout=min(0.1, max(0, deadline - time.monotonic())))
    else:
        raise AssertionError(f"heartbeat readiness check for {node} did not settle")
    from aiflow.v4.control.system_check import refresh_node
    refresh_node(ctx, node, "manual", credential_checks=credential_checks)
    return r.json()["desired"]


def candidates(client, headers, account="shared-01"):
    return [t["key"] for t in client.get("/v1/work/candidates", params={"account": account},
                                         headers=headers).json()["tickets"]]


def claim(client, headers, ticket, account="shared-01"):
    return client.post("/v1/work/claim", json={"ticket": ticket, "account": account}, headers=headers)


def test_unshared_account_skips_capacity_aggregation(monkeypatch):
    from aiflow.v4.control import shared_accounts
    from aiflow.v4.core.controlrepo import Account

    account = Account(id="claude-01", provider="claude", node="cc", parallel=2)
    monkeypatch.setattr(shared_accounts, "pending_nodes", lambda *_args: set())

    def unexpected(*_args, **_kwargs):
        raise AssertionError("unshared accounts do not need shared-capacity queries")

    monkeypatch.setattr(shared_accounts, "machine_capacities", unexpected)
    monkeypatch.setattr(shared_accounts, "running_by_node", unexpected)
    assert shared_accounts.may_take(object(), account, "cc") == (True, "")


def two_machines(client, ctx, limit=2):
    """Account `shared-01` created in Command Center, enabled on cc and node1, signed in on both."""
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    created = client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude", "session_limit": limit})
    assert created.status_code == 201, created.text
    for name in ("cc", "node1"):
        assert client.post("/v1/shared-accounts/shared-01/machines", json={"node": name}).status_code == 201
    for headers in (cc, node1):
        assert client.post("/v1/accounts/shared-01/probe", json={"ok": True}, headers=headers).status_code == 200
        beat(client, "cc" if headers is cc else "node1", headers)
    for key in ("TESTWP-1", "TESTWP-2", "TESTWP-3"):
        add_ticket(ctx.db, key)
    return cc, node1


def test_account_exists_apart_from_machines_and_node_agent_reports_its_provider_sign_in(client, ctx, monkeypatch,
                                                                                       tmp_path):
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    assert client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"}).status_code == 201
    account = client.get("/v1/shared-accounts").json()["accounts"]
    shared = next(a for a in account if a["id"] == "shared-01")
    assert shared["machines"] == []  # exists without any machine

    for name in ("cc", "node1"):
        enabled = client.post("/v1/shared-accounts/shared-01/machines", json={"node": name})
        assert enabled.status_code == 201
    assert client.post("/v1/shared-accounts/shared-01/machines", json={"node": "cc"}).status_code == 409
    state_account = next(a for a in client.get("/v1/state").json()["accounts"] if a["id"] == "shared-01")
    assert state_account["shared_managed"] is True
    assert {m["node"]: m["sign_in_pending"] for m in state_account["machines"]} == {"cc": True, "node1": True}
    assert all(m["shared_assignment"] for m in state_account["machines"])
    desired = beat(client, "node1", node1)
    handoff = desired["signin"][0]
    assert handoff["account"] == "shared-01" and handoff["provider"] == "claude"
    assert handoff["shared_account"] and handoff["request_id"]
    assert client.post("/v1/accounts/shared-01/signin-prompt", json={
        "link": "http://insecure.test", "code": "ABCD-2345"}, headers=node1).status_code == 422

    # The node agent launches the provider CLI in the account's private home and relays its device handoff.
    from aiflow.v4.node import agent
    provider_cli = ('print("Open https://claude.example.test/device and enter code ABCD-2345 "'
                    '+ "token sk-test-credential")')
    monkeypatch.setattr(agent, "ensure_account_home", lambda *_args: True)
    monkeypatch.setattr(agent, "SIGN_IN_COMMANDS", {"claude": [sys.executable, "-c", provider_cli]})
    monkeypatch.setattr(agent, "_SIGN_IN_RUN_AS", [])

    heartbeat_desired = {}
    prompt_reports = []
    completion_reported = Event()

    class NodeAPI:
        def __init__(self, *_args, **_kwargs):
            pass

        def call(self, method, path, body=None, **_kwargs):
            response = client.request(method, path, json=body, headers=node1)
            if not response.is_success:
                raise RuntimeError(f"{method} {path} -> {response.status_code}")
            result = response.json()
            if path.endswith("/heartbeat"):
                heartbeat_desired.update(result["desired"])
            if path.endswith("/signin-prompt"):
                prompt_reports.append(dict(body))
            if path.endswith("/signin-complete"):
                completion_reported.set()
            return result

    class StopAfterCompletion:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout=None):
            assert completion_reported.wait(timeout=5), "node agent did not report successful provider login"
            self.stopped = True

    from aiflow.v4.node import operations
    monkeypatch.setattr(agent, "ControlClient", NodeAPI)
    monkeypatch.setattr(agent, "tool_versions", lambda: {})
    monkeypatch.setattr(agent, "metrics", lambda prev: ({}, prev))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "_process_node_operation_logged", lambda *_args: None)
    monkeypatch.setattr(operations, "prepare_project_toolchains", lambda *_args: {})
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")
    monkeypatch.setenv("AIFLOW4_SELF_UPDATE", "0")
    agent.run_agent("https://control.example.test", "node1", "node-token", every_s=1, manage_workers=False,
                    stop=StopAfterCompletion())
    assert heartbeat_desired["signin"][0]["account"] == "shared-01"
    assert heartbeat_desired["signin"][0]["provider"] == "claude"
    assert prompt_reports == [{"link": "https://claude.example.test/device", "code": "ABCD-2345",
                               "expires_in_s": 900}]
    auth = ctx.db.one("SELECT status,last_probe_at,quota_source,quota_observed_at FROM accounts"
                      " WHERE id='shared-01'")
    assert auth["status"] == "idle" and auth["last_probe_at"]
    assert auth["quota_source"] == "" and auth["quota_observed_at"] is None

    machines = {m["node"]: m for m in next(a for a in client.get("/v1/shared-accounts").json()["accounts"]
                                             if a["id"] == "shared-01")["machines"]}
    assert machines["node1"]["signed_in"] is True and machines["node1"]["signin"] is None
    assert machines["cc"]["signed_in"] is False
    next_heartbeat = beat(client, "node1", node1)
    assert next_heartbeat["signin"] == []
    assert "shared-01" in next_heartbeat["accounts"]
    assert "shared-01" in next_heartbeat["workers"]

    # AC6: control keeps no credential, only the prompt (cleared after sign-in)
    stored = ctx.db.all("SELECT * FROM account_machines")
    assert "sk-test-credential" not in str(stored) + str(ctx.db.all("SELECT * FROM write_audit"))
    columns = {r["column_name"] for r in ctx.db.all(
        "SELECT column_name FROM information_schema.columns WHERE table_name IN ('shared_accounts','account_machines')")}
    assert not {c for c in columns if "token" in c or "secret" in c or "password" in c or "credential" == c}


def test_two_machines_share_one_session_limit_and_work_is_spread(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    assert [w for w in beat(client, "cc", cc)["workers"] if w.startswith("shared")] == [
        "shared-01", "shared-01.2"]  # each machine can carry the whole limit

    first = claim(client, cc, "TESTWP-1")
    assert first.status_code == 200 and first.json()["claimed"], first.text
    # cc has its fair share (1 of 2): the other machine takes the next ticket
    assert candidates(client, cc) == []
    assert claim(client, cc, "TESTWP-2").status_code == 409
    assert candidates(client, node1) == ["TESTWP-2", "TESTWP-3"]
    assert claim(client, node1, "TESTWP-2").json()["claimed"]

    # the total limit holds across machines
    assert candidates(client, cc) == [] and candidates(client, node1) == []
    refused = claim(client, node1, "TESTWP-3")
    assert refused.status_code == 409
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='shared-01' AND status='running'")["n"] == 2

    # usage is counted once per account, with a breakdown per machine below it
    ctx.db.run("UPDATE runs SET tokens_in=100,tokens_out=50,cost_usd=0.25"
               " WHERE account_id='shared-01' AND node='cc'")
    ctx.db.run("UPDATE runs SET tokens_in=200,tokens_out=75,cost_usd=0.50"
               " WHERE account_id='shared-01' AND node='node1'")
    accounts = [a for a in snapshot(ctx)["accounts"] if a["id"] == "shared-01"]
    assert len(accounts) == 1
    shared = accounts[0]
    assert shared["runs_active"] == 2 and shared["session_limit"] == 2 and shared["shared"]
    assert {m["node"]: m["runs_active"] for m in shared["machines"]} == {"cc": 1, "node1": 1}
    assert shared["tokens_today"] == 425 and shared["cost_today"] == 0.75
    assert {m["node"]: (m["tokens_today"], m["cost_today"]) for m in shared["machines"]} == {
        "cc": (150, 0.25), "node1": (275, 0.5)}

    # a finished session frees capacity for either machine
    run = first.json()
    assert client.post(f"/v1/runs/{run['run_id']}/release", json={"fence": run["fence"]}, headers=cc).status_code == 200
    ctx.db.run("UPDATE runs SET status='done' WHERE id=%s", (run["run_id"],))
    assert "TESTWP-3" in candidates(client, cc)


def test_concurrent_claims_never_exceed_the_total_limit(client, ctx):
    from aiflow.v4.control import shared_accounts
    cc, _ = two_machines(client, ctx, limit=1)
    acc = ctx.repo().account("shared-01")
    ids = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 4)]
    results = [shared_accounts.claim_run(ctx.db, acc, (rid, f"TESTWP-{i}", "TESTWP", "dev", "shared-01", "claude",
                                                       "", node, i, "x"))
               for i, (rid, node) in enumerate(zip(ids, ("cc", "node1", "cc")), start=1)]
    assert results == [True, False, False]


def test_simultaneous_claims_from_one_machine_cannot_take_another_online_machines_share(client, ctx):
    from aiflow.v4.control import shared_accounts

    cc, _node1 = two_machines(client, ctx, limit=2)
    acc = ctx.repo().account("shared-01")
    barrier = Barrier(3)

    def claim_on_cc(index):
        barrier.wait(timeout=5)
        run_id = f"00000000-0000-0000-0000-{index:012d}"
        return shared_accounts.claim_run(ctx.db, acc, (run_id, f"TESTWP-{index}", "TESTWP", "dev", "shared-01",
                                                       "claude", "", "cc", index, "x"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_on_cc, i) for i in (11, 12)]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results) == [False, True]
    assert shared_accounts.claim_run(ctx.db, acc, ("00000000-0000-0000-0000-000000000013", "TESTWP-13", "TESTWP",
                                                   "dev", "shared-01", "claude", "", "node1", 13, "x"))
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='shared-01' AND status='running'")["n"] == 2


def test_simultaneous_shared_account_claims_reserve_reported_quota(client, ctx):
    from aiflow.v4.control import shared_accounts
    from aiflow.v4.control.db import J

    cc, node1 = two_machines(client, ctx, limit=2)
    acc = ctx.repo().account("shared-01")
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}}]}
    ctx.db.run("UPDATE accounts SET quota_forecast=%s WHERE id='shared-01'", (J(forecast),))
    barrier = Barrier(3)

    def claim_on(index, node):
        barrier.wait(timeout=5)
        run_id = f"00000000-0000-0000-0000-{index:012d}"
        details = {"capacity": {"estimate": {"tokens": {"high": 60_000}}}}
        return shared_accounts.claim_run(
            ctx.db, acc,
            (run_id, f"TESTWP-QUOTA-{index}", "TESTWP", "dev", "shared-01", "claude", "", node, index, "x"),
            run_details=details, quota_reservation_tokens=60_000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_on, 31, "cc"), pool.submit(claim_on, 32, "node1")]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results) == [False, True]
    running = ctx.db.all("SELECT details FROM runs WHERE account_id='shared-01' AND status='running'")
    assert len(running) == 1
    assert running[0]["details"]["quota_reservation"] == {"tokens": 60_000}


def test_simultaneous_claims_include_reported_usage_after_the_last_quota_snapshot(client, ctx):
    from aiflow.v4.control import shared_accounts
    from aiflow.v4.control.db import J

    two_machines(client, ctx, limit=2)
    acc = ctx.repo().account("shared-01")
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}}]}
    ctx.db.run("UPDATE accounts SET quota_forecast=%s,quota_observed_at=%s WHERE id='shared-01'",
               (J(forecast), observed_at))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,ended_at,"
               "tokens_in,tokens_out) VALUES (%s,'TESTWP-QUOTA-PRIOR','TESTWP','dev','shared-01','claude','cc',1,"
               "'finished',now(),30_000,20_000)", ("00000000-0000-0000-0000-000000000099",))
    barrier = Barrier(3)

    def claim_on(index, node):
        barrier.wait(timeout=5)
        run_id = f"00000000-0000-0000-0000-{index:012d}"
        details = {"capacity": {"estimate": {"tokens": {"high": 30_000}}}}
        return shared_accounts.claim_run(
            ctx.db, acc,
            (run_id, f"TESTWP-QUOTA-UNOBSERVED-{index}", "TESTWP", "dev", "shared-01", "claude", "",
             node, index, "x"), run_details=details, quota_reservation_tokens=30_000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_on, 41, "cc"), pool.submit(claim_on, 42, "node1")]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results) == [False, True]
    running = ctx.db.all("SELECT details FROM runs WHERE account_id='shared-01' AND status='running'")
    assert len(running) == 1
    assert running[0]["details"]["quota_reservation"] == {"tokens": 30_000}


@pytest.mark.parametrize("status", ["lost", "released"])
def test_atomic_claim_keeps_unreported_terminal_reservation_until_a_newer_provider_snapshot(client, ctx, status):
    from aiflow.v4.control import shared_accounts
    from aiflow.v4.control.db import J

    two_machines(client, ctx, limit=2)
    acc = ctx.repo().account("shared-01")
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    terminal_at = observed_at + timedelta(minutes=1)
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "weekly", "left_tokens": 100_000, "prediction": {}}]}
    ctx.db.run("UPDATE accounts SET quota_forecast=%s,quota_observed_at=%s WHERE id='shared-01'",
               (J(forecast), observed_at))
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,ended_at,details)"
               " VALUES (%s,'TESTWP-TERMINAL-QUOTA','TESTWP','dev','shared-01','claude','cc',1,%s,%s,%s)",
               ("00000000-0000-0000-0000-000000000077", status, terminal_at,
                J({"quota_reservation": {"tokens": 50_000}})))
    values = ("00000000-0000-0000-0000-000000000078", "TESTWP-LOST-GUARD", "TESTWP", "dev", "shared-01",
              "claude", "", "cc", 78, "x")
    assert not shared_accounts.claim_run(ctx.db, acc, values, quota_reservation_tokens=60_000)

    ctx.db.run("UPDATE accounts SET quota_observed_at=%s WHERE id='shared-01'",
               (terminal_at + timedelta(seconds=1),))
    assert shared_accounts.claim_run(ctx.db, acc, values, quota_reservation_tokens=60_000)


def test_work_moves_to_the_other_machine_when_one_goes_offline(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    assert claim(client, cc, "TESTWP-1").json()["claimed"]
    assert candidates(client, cc) == []  # node1 is online and has free capacity: it takes the next one

    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '10 minutes' WHERE name='node1'")
    assert candidates(client, cc) == ["TESTWP-2", "TESTWP-3"]
    assert claim(client, cc, "TESTWP-2").json()["claimed"]
    assert claim(client, cc, "TESTWP-3").status_code == 409  # still never above the total of 2
    shared = next(a for a in snapshot(ctx)["accounts"] if a["id"] == "shared-01")
    assert {m["node"]: m["node_status"] for m in shared["machines"]}["node1"] == "offline"


def test_a_machine_that_has_not_signed_in_takes_no_work(client, ctx):
    sign_in(client, ctx)
    cc = join(client, "cc")
    client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude", "session_limit": 2})
    client.post("/v1/shared-accounts/shared-01/machines", json={"node": "cc"})
    add_ticket(ctx.db, "TESTWP-1")
    assert candidates(client, cc) == []
    assert claim(client, cc, "TESTWP-1").status_code == 409


def test_removing_one_machine_leaves_the_others_and_removing_all_needs_confirmation(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    assert claim(client, node1, "TESTWP-1").json()["claimed"]

    removed = client.delete("/v1/shared-accounts/shared-01/machines/cc")
    assert removed.status_code == 200 and removed.json()["account_removed"] is False
    assert removed.json()["machines"] == ["node1"]
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='shared-01' AND status='running'")["n"] == 1
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-2", "account": "shared-01"},
                       headers=cc).status_code == 403  # cc no longer runs the account
    assert candidates(client, node1) == ["TESTWP-2", "TESTWP-3"]  # node1 now has the whole limit
    assert not [w for w in beat(client, "cc", cc)["workers"] if w.startswith("shared-01")]

    # the last machine: asks for confirmation first, nothing changes without it
    asked = client.delete("/v1/shared-accounts/shared-01/machines/node1")
    assert asked.status_code == 409 and asked.json()["detail"]["error"] == "confirmation_required"
    assert ctx.db.one("SELECT 1 AS x FROM account_machines WHERE node='node1'")
    assert client.delete("/v1/shared-accounts/shared-01").status_code == 409
    gone = client.delete("/v1/shared-accounts/shared-01", params={"confirm": "true"})
    assert gone.status_code == 200 and gone.json()["removed"] is True
    assert not ctx.db.one("SELECT 1 AS x FROM shared_accounts WHERE id='shared-01'")
    assert not ctx.db.one("SELECT 1 AS x FROM accounts WHERE id='shared-01'")
    assert all(a["id"] != "shared-01" for a in client.get("/v1/shared-accounts").json()["accounts"])


def test_removing_the_last_machine_with_confirmation_removes_the_account(client, ctx):
    sign_in(client, ctx)
    join(client, "cc")
    client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"})
    client.post("/v1/shared-accounts/shared-01/machines", json={"node": "cc"})
    assert client.delete("/v1/shared-accounts/shared-01/machines/cc").status_code == 409
    done = client.delete("/v1/shared-accounts/shared-01/machines/cc", params={"confirm": "true"})
    assert done.status_code == 200 and done.json()["account_removed"] is True


def test_confirmed_account_removal_waits_for_the_claim_lock(client, ctx):
    from aiflow.v4.control import shared_accounts

    two_machines(client, ctx, limit=2)
    acc = ctx.repo().account("shared-01")
    started = Event()

    def remove_account():
        started.set()
        return client.delete("/v1/shared-accounts/shared-01", params={"confirm": "true"})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:shared-01",))
                future = pool.submit(remove_account)
                assert started.wait(timeout=5)
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline and not future.done():
                    blocked = bool(ctx.db.one("SELECT 1 AS waiting FROM pg_locks"
                                              " WHERE locktype='advisory' AND NOT granted"))
                    if blocked:
                        break
                    time.sleep(0.01)
                assert blocked, "account removal did not wait for the claim lock"
        removed = future.result(timeout=5)

    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert not ctx.db.one("SELECT 1 AS x FROM shared_accounts WHERE id='shared-01'")
    assert not ctx.db.one("SELECT 1 AS x FROM account_machines WHERE account_id='shared-01'")
    assert shared_accounts.claim_run(ctx.db, acc, ("00000000-0000-0000-0000-000000000099", "TESTWP-99", "TESTWP",
                                                   "dev", "shared-01", "claude", "", "cc", 99, "x")) is False


def test_account_from_the_control_repo_can_be_shared_with_another_machine(client, ctx):
    sign_in(client, ctx)
    cc = join(client, "cc")
    node1 = join(client, "node1")
    shared = client.post("/v1/shared-accounts/claude-01/machines", json={"node": "node1"})
    assert shared.status_code == 201
    assert shared.json()["session_limit"] == 1  # keeps its previous capacity until the owner changes it
    assert [m["node"] for m in shared.json()["machines"]] == ["cc", "node1"]
    assert all(m["removable"] for m in shared.json()["machines"])
    assert client.put("/v1/shared-accounts/claude-01/session-limit", json={"session_limit": 3}).json()["session_limit"] == 3
    beat(client, "cc", cc)
    beat(client, "node1", node1)
    assert client.post("/v1/accounts/claude-01/probe", json={"ok": True}, headers=node1).status_code == 200
    beat(client, "node1", node1)
    add_ticket(ctx.db, "TESTWP-1")

    # Removing the accounts.yaml primary detaches just that assignment; the other machine can still take work.
    removed = client.delete("/v1/shared-accounts/claude-01/machines/cc")
    assert removed.status_code == 200 and removed.json()["machines"] == ["node1"]
    account = ctx.repo().account("claude-01")
    assert account and account.runs_on("node1") and not account.runs_on("cc")
    assert candidates(client, node1, "claude-01") == ["TESTWP-1"]
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=node1)
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).status_code == 403

    # Removing the remaining configured assignment from all machines still asks for confirmation.
    asked = client.delete("/v1/shared-accounts/claude-01")
    assert asked.status_code == 409 and asked.json()["detail"]["error"] == "confirmation_required"
    all_removed = client.delete("/v1/shared-accounts/claude-01", params={"confirm": "true"})
    assert all_removed.status_code == 200 and all_removed.json()["source_retained"] is True
    assert ctx.repo().account("claude-01").machines == []
    detached = next(a for a in snapshot(ctx)["accounts"] if a["id"] == "claude-01")
    assert detached["machines"] == []
    assert detached["node"] == ""


def test_free_machine_capacity_moves_shared_work_and_weighted_share(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    # cc has one worker slot, while node1 has two. After cc uses its slot, only node1 can take more work.
    ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='cc'")
    ctx.db.run("UPDATE nodes SET slots_total=2 WHERE name='node1'")
    assert claim(client, cc, "TESTWP-1").json()["claimed"]
    assert candidates(client, cc) == []
    assert claim(client, cc, "TESTWP-2").status_code == 409
    assert candidates(client, node1) == ["TESTWP-2", "TESTWP-3"]
    assert claim(client, node1, "TESTWP-2").json()["claimed"]
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='shared-01' AND status='running'")["n"] == 2


def test_integer_machine_shares_respect_unequal_capacity_and_the_total_limit(client, ctx):
    from aiflow.v4.control.shared_accounts import capacity_shares, claim_run

    cc, node1 = two_machines(client, ctx, limit=4)
    ctx.db.run("UPDATE nodes SET slots_total=4 WHERE name='cc'")
    ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='node1'")
    add_ticket(ctx.db, "TESTWP-4")
    add_ticket(ctx.db, "TESTWP-5")

    assert capacity_shares(4, {"cc": 4, "node1": 1}) == {"cc": 3, "node1": 1}
    for key in ("TESTWP-1", "TESTWP-2", "TESTWP-3"):
        response = claim(client, cc, key)
        assert response.status_code == 200 and response.json()["claimed"]

    assert candidates(client, cc) == []  # three of four sessions is cc's integer share
    acc = ctx.repo().account("shared-01")
    fourth_on_cc = claim_run(ctx.db, acc, ("00000000-0000-0000-0000-000000000041", "TESTWP-4", "TESTWP", "dev",
                                            "shared-01", "claude", "", "cc", 41, "test"))
    assert fourth_on_cc is False  # locked claim enforces the same allocation as eligibility
    assert claim(client, node1, "TESTWP-4").json()["claimed"]
    assert claim(client, node1, "TESTWP-5").status_code == 409
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id='shared-01' AND status='running'")["n"] == 4


def test_machine_capacity_is_rechecked_atomically_during_claims(client, ctx):
    from aiflow.v4.control import shared_accounts

    cc, node1 = two_machines(client, ctx, limit=2)
    assert client.post("/v1/shared-accounts", json={"id": "shared-02", "provider": "claude",
                                                     "session_limit": 2}).status_code == 201
    for name in ("cc", "node1"):
        assert client.post("/v1/shared-accounts/shared-02/machines", json={"node": name}).status_code == 201
    for name, headers in (("cc", cc), ("node1", node1)):
        assert client.post("/v1/accounts/shared-02/probe", json={"ok": True}, headers=headers).status_code == 200
        beat(client, name, headers)
    ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='cc'")
    ctx.db.run("UPDATE nodes SET slots_total=0 WHERE name='node1'")
    barrier = Barrier(3)

    def claim_on_cc(account_id, index):
        barrier.wait(timeout=5)
        run_id = f"00000000-0000-0000-0000-{index:012d}"
        acc = ctx.repo().account(account_id)
        return shared_accounts.claim_run(ctx.db, acc, (run_id, f"TESTWP-{index}", "TESTWP", "dev", account_id,
                                                       "claude", "", "cc", index, "x"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim_on_cc, account_id, i)
                   for account_id, i in (("shared-01", 21), ("shared-02", 22))]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == [False, True]
    assert ctx.db.one("SELECT count(*) AS n FROM runs WHERE node='cc'"
                      " AND status='running'")["n"] == 1


def test_account_management_needs_an_owner_session(client, ctx):
    ctx.settings.console_session_secret = SESSION_SECRET
    assert client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"}).status_code == 401
    sign_in(client, ctx, role="lead")
    assert client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"}).status_code == 403
    assert client.get("/v1/shared-accounts").status_code == 200


def test_console_read_can_see_shared_account_usage_but_not_sign_in_prompts(client, ctx):
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"})
    client.post("/v1/shared-accounts/shared-01/machines", json={"node": "cc"})
    client.post("/v1/shared-accounts/shared-01/machines", json={"node": "node1"})
    prompt = {"link": "https://claude.example.test/device", "code": "ABCD-2345"}
    assert client.post("/v1/accounts/shared-01/signin-prompt", json=prompt, headers=node1).json()["stored"]

    owner_accounts = client.get("/v1/shared-accounts").json()["accounts"]
    owner_machine = next(a for a in owner_accounts if a["id"] == "shared-01")["machines"]
    assert next(m for m in owner_machine if m["node"] == "node1")["signin"]["code"] == "ABCD-2345"

    sign_in(client, ctx, role="lead")
    readonly_accounts = client.get("/v1/shared-accounts")
    assert readonly_accounts.status_code == 200
    readonly_machine = next(a for a in readonly_accounts.json()["accounts"] if a["id"] == "shared-01")["machines"]
    assert next(m for m in readonly_machine if m["node"] == "node1")["signin"] is None
    readonly_state = client.get("/v1/state")
    assert readonly_state.status_code == 200
    # Team Lead cannot see the Machines and AI view; the shared account is omitted from its state snapshot.
    assert not any(a["id"] == "shared-01" for a in readonly_state.json()["accounts"])
    assert "ABCD-2345" not in readonly_accounts.text + readonly_state.text


def test_auth_failure_on_one_machine_only_marks_that_machine_pending(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    failed = client.post("/v1/accounts/shared-01/probe", json={"ok": False, "error_kind": "auth", "summary": "401"},
                         headers=node1)
    assert failed.status_code == 200
    shared = next(a for a in client.get("/v1/shared-accounts").json()["accounts"] if a["id"] == "shared-01")
    assert {m["node"]: m["signed_in"] for m in shared["machines"]} == {"cc": True, "node1": False}
    # no account-wide cooldown: the healthy machine keeps taking work, and the failed one takes none
    assert ctx.db.one("SELECT cooldown_until FROM accounts WHERE id='shared-01'")["cooldown_until"] is None
    assert "TESTWP-1" in candidates(client, cc)
    assert claim(client, cc, "TESTWP-1").json()["claimed"]
    assert claim(client, cc, "TESTWP-2").json()["claimed"]  # node1 left the fair share: cc may use both slots
    assert claim(client, node1, "TESTWP-3").status_code == 409
    # the failed machine is asked to sign in again
    assert beat(client, "node1", node1)["signin"][0]["account"] == "shared-01"


def test_auth_failure_reported_by_a_run_does_not_cool_down_healthy_machine(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    claimed = claim(client, node1, "TESTWP-1")
    assert claimed.status_code == 200 and claimed.json()["claimed"]
    result = client.post(f"/v1/runs/{claimed.json()['run_id']}/result",
                         json={"fence": claimed.json()["fence"], "outcome": "failed", "error_kind": "auth",
                               "summary": "provider rejected this machine's credentials"}, headers=node1)
    assert result.status_code == 200
    assert ctx.leases.cooling("shared-01") is None
    assert ctx.db.one("SELECT status FROM account_machines WHERE account_id='shared-01' AND node='node1'")["status"] == "pending"
    assert claim(client, cc, "TESTWP-2").json()["claimed"]
    assert claim(client, cc, "TESTWP-3").json()["claimed"]
    assert beat(client, "node1", node1)["signin"][0]["account"] == "shared-01"


def test_run_auth_and_quota_updates_are_kept_while_a_sibling_run_is_active(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    failed_run = claim(client, node1, "TESTWP-1").json()
    sibling_run = claim(client, cc, "TESTWP-2").json()
    assert failed_run["claimed"] and sibling_run["claimed"]

    quota = {"windows": {"five_hour": {"utilization": 0.25}}}
    result = client.post(
        f"/v1/runs/{failed_run['run_id']}/result",
        json={"fence": failed_run["fence"], "outcome": "failed", "error_kind": "auth",
              "summary": "provider rejected this machine's credentials", "quota": quota},
        headers=node1,
    )

    assert result.status_code == 200
    assert ctx.db.one("SELECT quota FROM accounts WHERE id='shared-01'")["quota"] == quota
    assert ctx.db.one("SELECT status FROM account_machines WHERE account_id='shared-01' AND node='node1'")[
        "status"] == "pending"
    assert ctx.db.one("SELECT status FROM accounts WHERE id='shared-01'")["status"] == "working"
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (sibling_run["run_id"],))["status"] == "running"


def test_failed_sign_in_can_be_retried_with_a_fresh_request(client, ctx):
    import threading
    from unittest.mock import patch

    from aiflow.v4.node import agent
    sign_in(client, ctx)
    node1 = join(client, "node1")
    client.post("/v1/shared-accounts", json={"id": "shared-01", "provider": "claude"})
    client.post("/v1/shared-accounts/shared-01/machines", json={"node": "node1"})
    first = beat(client, "node1", node1)["signin"][0]
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()  # the provider login exited without success: its thread is dead
    active = {"shared-01": (first["request_id"], threading.Event(), dead)}
    started = []
    with patch.object(agent, "run_provider_sign_in", lambda *a, **k: started.append(a[2]["request_id"])):
        agent.sync_provider_sign_ins(None, "node1", [first], active)
        assert started == []  # same attempt: not restarted by itself
        retried = client.post("/v1/shared-accounts/shared-01/machines/node1/signin")
        assert retried.status_code == 200
        second = beat(client, "node1", node1)["signin"][0]
        assert second["request_id"] != first["request_id"]
        agent.sync_provider_sign_ins(None, "node1", [second], active)
        active["shared-01"][2].join(timeout=2)
    assert started == [second["request_id"]]
    # a machine that is signed in has nothing to retry
    client.post("/v1/accounts/shared-01/probe", json={"ok": True}, headers=node1)
    assert client.post("/v1/shared-accounts/shared-01/machines/node1/signin").status_code == 409


def test_quota_exhaustion_stays_an_account_wide_cooldown(client, ctx):
    cc, _node1 = two_machines(client, ctx, limit=2)
    client.post("/v1/accounts/shared-01/probe", json={"ok": False, "error_kind": "quota", "summary": "q"}, headers=cc)
    assert ctx.db.one("SELECT cooldown_until FROM accounts WHERE id='shared-01'")["cooldown_until"] is not None


def test_quota_routing_finds_shared_peer_enabled_only_on_a_secondary_machine(client, ctx):
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    beat(client, "cc", cc)
    beat(client, "node1", node1)
    created = client.post("/v1/shared-accounts", json={"id": "secondary-peer", "provider": "claude",
                                                       "roles": ["dev"], "session_limit": 1})
    assert created.status_code == 201
    assert client.post("/v1/shared-accounts/secondary-peer/machines", json={"node": "node1"}).status_code == 201
    assert client.post("/v1/accounts/secondary-peer/probe", json={"ok": True}, headers=node1).status_code == 200
    add_ticket(ctx.db, "TESTWP-QUOTA-SECONDARY")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='claude-01'")

    assert ctx.db.one("SELECT node FROM accounts WHERE id='secondary-peer'")["node"] == ""
    assert candidates(client, cc, "claude-01") == []
    assert candidates(client, node1, "secondary-peer") == ["TESTWP-QUOTA-SECONDARY"]


def test_quota_routing_does_not_defer_to_a_pending_shared_peer(client, ctx):
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    beat(client, "cc", cc)
    beat(client, "node1", node1)
    assert client.post("/v1/shared-accounts", json={"id": "pending-peer", "provider": "claude",
                                                    "roles": ["dev"], "session_limit": 1}).status_code == 201
    assert client.post("/v1/shared-accounts/pending-peer/machines", json={"node": "node1"}).status_code == 201
    add_ticket(ctx.db, "TESTWP-QUOTA-PENDING")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='claude-01'")

    assert candidates(client, cc, "claude-01") == ["TESTWP-QUOTA-PENDING"]


def test_quota_routing_does_not_defer_to_a_full_shared_peer(client, ctx):
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    beat(client, "cc", cc)
    beat(client, "node1", node1)
    assert client.post("/v1/shared-accounts", json={"id": "full-peer", "provider": "claude",
                                                    "roles": ["dev"], "session_limit": 1}).status_code == 201
    assert client.post("/v1/shared-accounts/full-peer/machines", json={"node": "node1"}).status_code == 201
    assert client.post("/v1/accounts/full-peer/probe", json={"ok": True}, headers=node1).status_code == 200
    add_ticket(ctx.db, "TESTWP-PEER-FILL")
    assert claim(client, node1, "TESTWP-PEER-FILL", "full-peer").json()["claimed"]
    add_ticket(ctx.db, "TESTWP-QUOTA-FULL")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='claude-01'")

    assert candidates(client, cc, "claude-01") == ["TESTWP-QUOTA-FULL"]


def test_quota_routing_does_not_defer_to_a_full_configured_peer(client, ctx):
    """A peer from accounts.yaml has no stored session limit; its parallel sessions still bound it."""
    sign_in(client, ctx)
    cc, node1 = join(client, "cc"), join(client, "node1")
    beat(client, "cc", cc)
    beat(client, "node1", node1)
    assert client.post("/v1/shared-accounts", json={"id": "src-01", "provider": "claude",
                                                    "roles": ["dev"], "session_limit": 1}).status_code == 201
    assert client.post("/v1/shared-accounts/src-01/machines", json={"node": "node1"}).status_code == 201
    assert client.post("/v1/accounts/src-01/probe", json={"ok": True}, headers=node1).status_code == 200
    add_ticket(ctx.db, "TESTWP-CFG-FILL")
    assert claim(client, cc, "TESTWP-CFG-FILL", "claude-01").json()["claimed"]
    add_ticket(ctx.db, "TESTWP-CFG-QUOTA")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='src-01'")

    assert candidates(client, node1, "src-01") == ["TESTWP-CFG-QUOTA"]


def test_pending_shared_assignment_gets_no_worker_until_signed_in(client, ctx):
    sign_in(client, ctx)
    node1 = join(client, "node1")
    assert client.post("/v1/shared-accounts", json={"id": "wait-01", "provider": "claude",
                                                    "session_limit": 1}).status_code == 201
    assert client.post("/v1/shared-accounts/wait-01/machines", json={"node": "node1"}).status_code == 201
    desired = beat(client, "node1", node1)
    assert "wait-01" not in desired["accounts"] and "wait-01" not in desired["workers"]
    assert [s["account"] for s in desired["signin"]] == ["wait-01"]
    assert client.post("/v1/accounts/wait-01/probe", json={"ok": True}, headers=node1).status_code == 200
    desired = beat(client, "node1", node1)
    assert "wait-01" in desired["accounts"] and "wait-01" in desired["workers"]


def test_session_limit_cannot_drop_below_active_sessions(client, ctx):
    cc, node1 = two_machines(client, ctx, limit=2)
    assert claim(client, cc, "TESTWP-1").json()["claimed"]
    assert claim(client, node1, "TESTWP-2").json()["claimed"]
    low = client.put("/v1/shared-accounts/shared-01/session-limit", json={"session_limit": 1})
    assert low.status_code == 409
    assert next(a for a in client.get("/v1/shared-accounts").json()["accounts"]
                if a["id"] == "shared-01")["session_limit"] == 2
    assert client.put("/v1/shared-accounts/shared-01/session-limit", json={"session_limit": 3}).status_code == 200


def test_claim_uses_the_current_limit_not_a_stale_one(client, ctx):
    from aiflow.v4.control import shared_accounts
    two_machines(client, ctx, limit=3)
    stale = ctx.repo().account("shared-01")  # read while the limit was 3
    assert client.put("/v1/shared-accounts/shared-01/session-limit", json={"session_limit": 1}).status_code == 200
    ok = [shared_accounts.claim_run(ctx.db, stale, (f"00000000-0000-0000-0000-0000000000{i}0", f"TESTWP-{i}", "TESTWP",
                                                    "dev", "shared-01", "claude", "", "cc", i, "x")) for i in (1, 2)]
    assert ok == [True, False]
