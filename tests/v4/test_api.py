import time

from conftest import add_ticket


def join(client, name, ip=""):
    r = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret", "ip": ip})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def test_join_requires_the_join_token(client):
    assert client.post("/v1/nodes/join", json={"name": "cc", "join_token": "nope"}).status_code == 403
    assert client.get("/v1/work/candidates", params={"account": "claude-01"}).status_code == 401


def test_node_heartbeat_is_shipped_to_openobserve_only(client, ctx):
    ctx.events.o2_url = "http://o2.test"  # queue only; the shipping thread is not started
    cc = join(client, "cc")
    response = client.post("/v1/nodes/cc/heartbeat", json={
        "slots_total": 4, "slots_used": 2, "release_version": "4.0.0", "metrics": {"cpu_pct": 12.5}
    }, headers=cc)

    assert response.status_code == 200
    shipped = [e for e in list(ctx.events.q.queue) if e["type"] == "node.heartbeat"]
    assert len(shipped) == 1
    assert shipped[0]["node"] == "cc" and shipped[0]["outcome"] == "online"
    assert shipped[0]["slots_total"] == 4 and shipped[0]["metrics"] == {"cpu_pct": 12.5}
    assert isinstance(shipped[0]["_timestamp"], int)
    assert ctx.db.one("SELECT count(*) AS n FROM events WHERE type='node.heartbeat'")["n"] == 0


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


def test_node_without_ai_accounts_is_flagged(client, ctx):
    from aiflow.v4.control.loops import watchdog
    n3 = join(client, "node3")
    client.post("/v1/nodes/node3/heartbeat", json={"slots_total": 2}, headers=n3)
    watchdog(ctx)
    inc = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='node_no_ai' AND subject='node3' AND closed_at IS NULL")
    assert inc["severity"] == "warning" and "no AI account" in inc["title"] and "not described" in inc["detail"]
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
    assert inc["severity"] == "warning" and inc["title"] == "role verify has 1 ready account"


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


def test_node_back_online_resolves_and_alerts(client, ctx, monkeypatch):
    from aiflow.v4.control import notify
    from aiflow.v4.control.loops import watchdog
    sent = []
    monkeypatch.setattr(notify, "send", lambda sev, title, detail="", link="": sent.append((sev, title)) or ["test"])
    n1 = join(client, "node1")
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=n1)
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '5 minutes' WHERE name='node1'")
    watchdog(ctx)
    acc = next(a for a in client.get("/v1/state").json()["accounts"] if a["id"] == "codex-01")
    assert acc["status"] == "node_offline"
    client.post("/v1/nodes/node1/heartbeat", json={"slots_total": 2}, headers=n1)
    assert ("critical", "node node1 is down") in sent and ("info", "resolved: node node1 is down") in sent
