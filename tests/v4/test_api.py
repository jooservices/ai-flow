import time
import uuid
from datetime import datetime, timedelta, timezone

from conftest import add_ticket
from aiflow.v4.control import api as control_api
from aiflow.v4.control.db import J


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
    run_id = uuid.uuid4()
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,error_kind,details)"
               " VALUES (%s,'TESTWP-51','TESTWP','dev','claude-01','cc',1,'finished','failed','network',%s)",
               (run_id, J({"retry": True, "error_message": "connection reset"})))
    sa = client.get("/v1/state").json()["sa"]
    sources = {issue["source"] for issue in sa["network_issues"]}
    assert sources == {"event", "run"}
    event = next(issue for issue in sa["network_issues"] if issue["source"] == "event")
    run = next(issue for issue in sa["network_issues"] if issue["source"] == "run")
    assert event["data"]["error_kind"] == "network" and event["data"]["retry"] is True
    assert run["ticket"] == "TESTWP-51" and run["error_kind"] == "network" and run["retry"] is True


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


def test_triage_candidates_follow_verify_and_exclude_last_dev_and_escalated(client, ctx):
    cc = join(client, "cc")
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-63", status="Needs owner", labels=("aiflow-ready",))
    add_ticket(ctx.db, "TESTWP-64", status="Needs owner", labels=("owner-escalated",))
    add_ticket(ctx.db, "TESTWP-65", status="In Verify")
    add_ticket(ctx.db, "TESTWP-66", status="To Do")
    add_ticket(ctx.db, "TESTWP-67", status="Needs owner")

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


def test_failed_triage_backs_off_before_the_ticket_is_offered_again(client, ctx):
    n1 = join(client, "node1")
    add_ticket(ctx.db, "TESTWP-67", status="Needs owner")

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
    assert report["tickets"] == {"total": 6, "done": 6, "done_by_type": {"Story": 3, "Task": 3}}
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
    assert "# Project report: TESTWP-100" in markdown.text
    assert "| Story | 3 |" in markdown.text and "median 4.5d, p90 7.0d (n=6)" in markdown.text
    assert "Tokens: 280 (200 in, 80 out)" in markdown.text and "TESTWP-102 · Summary of TESTWP-102" in markdown.text
    assert client.get("/v1/projects/UNKNOWN/report").status_code == 404
    assert client.get("/v1/projects/TESTWP/report", params={"epic": "TESTWP-101"}).status_code == 404
    assert client.get("/v1/projects/TESTWP/report", params={"format": "html"}).status_code == 400


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
    monkeypatch.setattr(notify, "send", lambda sev, title, detail="", link="": sent.append((sev, title)) or ["test"])
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
    assert w.account == "codex-01" and w.instance == "codex-01.2" and w.probe_ok
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
