"""Shared AI accounts: one account enabled on several machines with one total session limit (JAIF-246)."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

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
    r = client.post(f"/v1/nodes/{node}/heartbeat", json={"slots_total": 4}, headers=headers)
    assert r.status_code == 200
    return r.json()["desired"]


def candidates(client, headers, account="shared-01"):
    return [t["key"] for t in client.get("/v1/work/candidates", params={"account": account},
                                         headers=headers).json()["tickets"]]


def claim(client, headers, ticket, account="shared-01"):
    return client.post("/v1/work/claim", json={"ticket": ticket, "account": account}, headers=headers)


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
    prompt_reported = Event()

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
                prompt_reported.set()
            return result

    class StopAfterPrompt:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _timeout=None):
            assert prompt_reported.wait(timeout=5), "node agent did not report the provider prompt"
            time.sleep(0.1)  # let the short provider CLI exit before run_agent cancels its sign-in threads
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
                    stop=StopAfterPrompt())
    assert heartbeat_desired["signin"][0]["account"] == "shared-01"
    assert heartbeat_desired["signin"][0]["provider"] == "claude"

    machines = {m["node"]: m for m in next(a for a in client.get("/v1/shared-accounts").json()["accounts"]
                                             if a["id"] == "shared-01")["machines"]}
    assert machines["node1"]["signed_in"] is False
    assert machines["node1"]["signin"]["code"] == "ABCD-2345"
    assert machines["node1"]["signin"]["link"] == "https://claude.example.test/device"

    # signing in once on the machine (a passing check with its own credentials) completes it
    assert client.post("/v1/accounts/shared-01/probe", json={"ok": True}, headers=node1).status_code == 200
    machines = {m["node"]: m for m in next(a for a in client.get("/v1/shared-accounts").json()["accounts"]
                                             if a["id"] == "shared-01")["machines"]}
    assert machines["node1"]["signed_in"] is True and machines["node1"]["signin"] is None
    assert machines["cc"]["signed_in"] is False
    assert beat(client, "node1", node1)["signin"] == []

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
    assert first.status_code == 200 and first.json()["claimed"]
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
    accounts = [a for a in snapshot(ctx)["accounts"] if a["id"] == "shared-01"]
    assert len(accounts) == 1
    shared = accounts[0]
    assert shared["runs_active"] == 2 and shared["session_limit"] == 2 and shared["shared"]
    assert {m["node"]: m["runs_active"] for m in shared["machines"]} == {"cc": 1, "node1": 1}

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


def test_account_from_the_control_repo_can_be_shared_with_another_machine(client, ctx):
    sign_in(client, ctx)
    join(client, "cc")
    node1 = join(client, "node1")
    shared = client.post("/v1/shared-accounts/claude-01/machines", json={"node": "node1"})
    assert shared.status_code == 201
    assert shared.json()["session_limit"] == 1  # keeps its previous capacity until the owner changes it
    assert [m["node"] for m in shared.json()["machines"]] == ["cc", "node1"]
    assert client.put("/v1/shared-accounts/claude-01/session-limit", json={"session_limit": 3}).json()["session_limit"] == 3
    # the repo-defined machine cannot be removed here
    assert client.delete("/v1/shared-accounts/claude-01/machines/cc").status_code == 409
    assert client.delete("/v1/shared-accounts/claude-01/machines/node1").status_code == 200
    assert [m["node"] for m in client.get("/v1/shared-accounts").json()["accounts"][0]["machines"]] == ["cc"]


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
    readonly_shared = next(a for a in readonly_state.json()["accounts"] if a["id"] == "shared-01")
    assert next(m for m in readonly_shared["machines"] if m["node"] == "node1")["signin"] is None
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


def test_quota_exhaustion_stays_an_account_wide_cooldown(client, ctx):
    cc, _node1 = two_machines(client, ctx, limit=2)
    client.post("/v1/accounts/shared-01/probe", json={"ok": False, "error_kind": "quota", "summary": "q"}, headers=cc)
    assert ctx.db.one("SELECT cooldown_until FROM accounts WHERE id='shared-01'")["cooldown_until"] is not None


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
