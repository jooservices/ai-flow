import time

from conftest import add_ticket


def join(client, name):
    r = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def test_join_requires_the_join_token(client):
    assert client.post("/v1/nodes/join", json={"name": "cc", "join_token": "nope"}).status_code == 403
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}).status_code == 401


def test_full_claim_flow_with_fencing(client, ctx):
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

    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["fence"] == 1 and r["model"] == "opus" and r["provider"] == "claude"
    assert r["project"]["repo"] == "jooservices/test-aiflow-wp" and r["identity"][0].endswith("[bot]")
    run_id, fence = r["run_id"], r["fence"]
    # second claim of the same ticket is refused (held), candidate list no longer shows it
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "claude-01"}, headers=cc).status_code == 409
    assert [t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=cc).json()["tickets"]] == ["TESTWP-2"]

    assert client.post(f"/v1/runs/{run_id}/heartbeat", json={"fence": fence, "progress": {"phase": "ai"}}, headers=cc).status_code == 200
    assert client.post("/v1/jira/TESTWP-1/transition", json={"run_id": run_id, "fence": fence, "to": "In Progress"},
                       headers=cc).json()["status"] == "In Progress"
    assert client.post("/v1/github/token", json={"run_id": run_id, "fence": fence, "repo": "jooservices/test-aiflow-wp"},
                       headers=cc).json()["token"] == "ghs_fake"
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
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "metrics": {"cpu_pct": 10}}, headers=cc)
    add_ticket(ctx.db, "TESTWP-9")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=cc).json()
    ctx.jira.status_of["TESTWP-9"] = "In Progress"
    s = client.get("/v1/state").json()
    assert any(n["name"] == "cc" and n["status"] == "online" for n in s["nodes"])
    assert s["active_runs"][0]["ticket"] == "TESTWP-9"
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
    assert client.post("/v1/accounts/claude-01/probe", json={"ok": True, "quota": q, "summary": "OK"}, headers=cc).status_code == 200
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
