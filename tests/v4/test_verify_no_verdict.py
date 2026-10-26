"""Epics are never offered as work; a verify run without a verdict moves to another account (JAIF-271)."""
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

from conftest import add_open_escalation, add_ticket, join_node
from aiflow.v4.control import incidents, role_health, state, verify_retry, work_candidates, work_eligibility
from aiflow.v4.control.db import J
from aiflow.v4.core.controlrepo import Account
from aiflow.v4.control.loops import _reconcile_epics, watchdog
from aiflow.v4.control.repositories.runs import RunsRepository

PR = "https://github.com/jooservices/test-aiflow-wp/pull/7"


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['node_token']}"}
    assert client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2}, headers=headers).status_code == 200
    return headers


def offered(client, headers, account):
    r = client.get("/v1/work/candidates", params={"account": account}, headers=headers)
    assert r.status_code == 200
    return {(t["key"], t["step"]) for t in r.json()["tickets"]}


def insert_run(ctx, key, step, account, outcome, details=None, minutes_ago=10, **extra):
    started = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ctx.db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,status,outcome,"
        "started_at,ended_at,pr_url,summary,details) VALUES (%s,%s,'TESTWP',%s,%s,'x','m','cc',1,'finished',%s,%s,%s,%s,%s,%s)",
        (str(uuid.uuid4()), key, step, account, outcome, started, started + timedelta(minutes=1),
         extra.get("pr_url", ""), extra.get("summary", ""), J(details or {})))


def in_verify(ctx, key="TESTWP-50"):
    add_ticket(ctx.db, key, "In Verify")
    insert_run(ctx, key, "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60)


def no_verdict(ctx, key, account, minutes_ago=5):
    insert_run(ctx, key, "verify", account, "failed", {"no_verdict": True, "verdict": ""},
               minutes_ago=minutes_ago, summary="no verdict (missing VERDICT line): x")


def test_epic_with_ready_label_is_never_offered(client, ctx):
    ctx.repo().account("claude-01").roles.extend(("intake_refiner", "master"))
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-60")  # an ordinary ready ticket is offered
    add_ticket(ctx.db, "TESTWP-61", "To Do", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-61'")
    add_ticket(ctx.db, "TESTWP-62", "Intake", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-62'")
    in_verify(ctx, "TESTWP-63")
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-63'")
    add_ticket(ctx.db, "TESTWP-64", "Needs owner", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-64'")
    add_open_escalation(ctx.db, "TESTWP-64")
    add_ticket(ctx.db, "TESTWP-65", "To Do", labels=("aiflow-ready",))
    ctx.db.run("UPDATE tickets SET type='epic' WHERE key='TESTWP-65'")
    seen = offered(client, cc, "claude-01")
    assert ("TESTWP-60", "dev") in seen
    assert not {key for key, _ in seen} & {"TESTWP-61", "TESTWP-62", "TESTWP-63", "TESTWP-64", "TESTWP-65"}
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-61", "account": "claude-01"},
                       headers=cc).status_code == 409


def test_idle_flow_checks_get_ticket_rows_from_repository_and_skip_epics(ctx):
    add_ticket(ctx.db, "TESTWP-IDLE-ORDINARY")
    add_ticket(ctx.db, "TESTWP-IDLE-EPIC")
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-IDLE-EPIC'")
    add_ticket(ctx.db, "TESTWP-IDLE-VERIFY", "In Verify")
    add_ticket(ctx.db, "TESTWP-IDLE-VERIFY-EPIC", "In Verify")
    ctx.db.run("UPDATE tickets SET type='epic' WHERE key='TESTWP-IDLE-VERIFY-EPIC'")

    expected = {"TESTWP-IDLE-ORDINARY", "TESTWP-IDLE-VERIFY"}

    assert {row["key"] for row in incidents.idle_tickets(ctx)} == expected
    assert {row["key"] for row in incidents.workable_tickets(ctx)} == expected


def test_registered_candidate_rule_names_why_an_epic_is_ineligible():
    decision = work_eligibility.evaluate_candidate(
        None, {"key": "TESTWP-66", "type": "Epic"}, ("not_epic",))

    assert not decision.eligible
    assert decision.rejected_by == "not_epic"
    assert "cannot be offered as work" in decision.reason
    assert {strategy.name for strategy in work_candidates._ROLE_STRATEGIES.values()} == {
        "verification", "post_merge", "triage", "intake", "impact", "development",
    }


def test_epic_follows_its_children(ctx):
    add_ticket(ctx.db, "TESTWP-70", "To Do", labels=())
    ctx.db.run("UPDATE tickets SET type='epic' WHERE key='TESTWP-70'")
    for key, status in (("TESTWP-71", "To Do"), ("TESTWP-72", "To Do")):
        add_ticket(ctx.db, key, status, labels=())
        ctx.db.run("UPDATE tickets SET type='Story', parent_key='TESTWP-70' WHERE key=%s", (key,))

    def reconcile():
        _reconcile_epics(ctx, ["TESTWP"], set(), set(), set())
        return ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-70'")["status"]

    assert reconcile() == "To Do"
    ctx.db.run("UPDATE tickets SET status='In Progress' WHERE key='TESTWP-71'")
    assert reconcile() == "In Progress"
    ctx.db.run("UPDATE tickets SET status='Done' WHERE key='TESTWP-71'")
    assert reconcile() == "In Progress"
    ctx.db.run("UPDATE tickets SET status='Declined' WHERE key='TESTWP-72'")
    assert reconcile() == "Done"


def test_no_verdict_run_goes_to_another_account_at_once(client, ctx):
    cc, n1 = join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    assert offered(client, cc, "claude-01") >= {("TESTWP-50", "verify")}
    assert offered(client, n1, "codex-01") >= {("TESTWP-50", "verify")}
    no_verdict(ctx, "TESTWP-50", "codex-01")
    # no waiting time: the other account has the ticket now, the account that failed does not
    assert ("TESTWP-50", "verify") in offered(client, cc, "claude-01")
    assert ("TESTWP-50", "verify") not in offered(client, n1, "codex-01")
    # every eligible account has tried once: the first may try again, and it is not barred yet
    no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=4)
    assert ("TESTWP-50", "verify") in offered(client, n1, "codex-01")
    # two in a row: codex-01 is not offered the ticket again, claude-01 still is
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=3)
    assert ("TESTWP-50", "verify") not in offered(client, n1, "codex-01")
    assert ("TESTWP-50", "verify") in offered(client, cc, "claude-01")


def test_newer_no_verdict_breaks_an_ordinary_verify_cooldown(client, ctx):
    cc, node1 = join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    for minutes in (9, 6, 3):
        insert_run(ctx, "TESTWP-50", "verify", "claude-01", "failed", minutes_ago=minutes)
    assert "TESTWP-50" in verify_retry.cooling(ctx.db)

    no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=1)

    assert "TESTWP-50" not in verify_retry.cooling(ctx.db)
    assert ("TESTWP-50", "verify") in offered(client, node1, "codex-01")


def test_retry_probe_returns_alternate_with_ready_development_work(client, ctx):
    cc = join(client, "cc")
    join(client, "node1")
    in_verify(ctx)
    add_ticket(ctx.db, "TESTWP-51", "To Do", labels=("aiflow-ready",))
    no_verdict(ctx, "TESTWP-50", "codex-01")

    # The capacity router also checks the peer's development offers. Its verify probe must keep the retry guard
    # disabled throughout those nested checks so the untried account can receive the ticket immediately.
    assert ("TESTWP-50", "verify") in offered(client, cc, "claude-01")


def test_no_verdict_counts_against_the_verify_role_only_from_the_second_in_a_row(ctx):
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=6)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=3)
    runs = {r["started_at"]: r for r in RunsRepository(ctx.db).role_health_history(["verify"], "codex-01", 10)}
    first, second = (runs[key] for key in sorted(runs))
    assert role_health.classify(first).failure is None
    assert role_health.classify(second).failure == 1.0
    # a verdict in between breaks the streak
    insert_run(ctx, "TESTWP-50", "verify", "codex-01", "rework", {"verdict": "FAIL"}, minutes_ago=2)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=1)
    latest = max(RunsRepository(ctx.db).role_health_history(["verify"], "codex-01", 10),
                 key=lambda r: r["started_at"])
    assert role_health.classify(latest).failure is None


def test_prior_no_verdict_from_before_new_development_round_is_ignored(ctx):
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=55)
    insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=30)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=20)

    latest = max(RunsRepository(ctx.db).role_health_history(["verify"], "codex-01", 10),
                 key=lambda row: row["started_at"])
    assert latest["prev_no_verdict"] is False
    assert role_health.classify(latest).failure is None


def test_no_verdict_streak_does_not_open_generic_ticket_stuck_incident(client, ctx):
    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=8)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=6)
    no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=4)

    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='ticket_stuck' AND subject='TESTWP-50'")["n"] == 0
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict' AND subject='TESTWP-50'")["n"] == 0

    no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=2)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='ticket_stuck' AND subject='TESTWP-50'")["n"] == 0
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict' AND subject='TESTWP-50'")["n"] == 1


def test_failure_on_all_eligible_accounts_raises_one_incident_and_the_dashboard_lists_it(client, ctx):
    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    assert not verify_retry.exhausted(ctx, {"key": "TESTWP-50", "project": "TESTWP", "status": "In Verify",
                                           "labels": ["aiflow-ready"]})
    for account in ("codex-01", "claude-01"):
        no_verdict(ctx, "TESTWP-50", account, minutes_ago=8)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'")["n"] == 0
    for account in ("codex-01", "claude-01"):
        no_verdict(ctx, "TESTWP-50", account, minutes_ago=2)
    watchdog(ctx)
    watchdog(ctx)
    rows = ctx.db.all("SELECT subject, title, detail FROM incidents WHERE kind='verify_no_verdict'"
                      " AND closed_at IS NULL")
    assert len(rows) == 1 and rows[0]["subject"] == "TESTWP-50"
    assert "TESTWP-50" in rows[0]["title"] and "without a VERDICT line" in rows[0]["detail"]
    assert "claude-01" not in rows[0]["detail"] and "codex-01" not in rows[0]["detail"]
    # the Dashboard lists the ticket with the accounts that tried
    slow = state.snapshot(ctx)["slow_verifications"]
    assert [v["key"] for v in slow] == ["TESTWP-50"]
    assert {a["account"]: a["barred"] for a in slow[0]["accounts"]} == {"claude-01": True, "codex-01": True}
    # a new development round starts the count again and closes the incident
    insert_run(ctx, "TESTWP-50", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=0)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                      " AND closed_at IS NULL")["n"] == 0


def test_stale_exhausted_ticket_opens_only_verify_incident_not_flow_idle(client, ctx):
    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    ctx.db.run("UPDATE tickets SET jira_updated=now()-interval '2 hours' WHERE key='TESTWP-50'")
    for account in ("codex-01", "claude-01"):
        for minutes in (40, 35):
            no_verdict(ctx, "TESTWP-50", account, minutes_ago=minutes)

    for _ in range(2):
        watchdog(ctx)
        incidents_by_kind = ctx.db.all(
            "SELECT kind, count(*) AS count FROM incidents WHERE subject='TESTWP-50' AND closed_at IS NULL"
            " GROUP BY kind")
        assert incidents_by_kind == [{"kind": "verify_no_verdict", "count": 1}]
    assert "TESTWP-50" not in {row["key"] for row in incidents.idle_tickets(ctx)}


def test_loops_watchdog_initializes_candidate_probe_without_create_app(ctx, tmp_path):
    from aiflow.v4.control import loops

    assert ctx.candidate_offers is None
    in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        for minutes in (8, 2):
            no_verdict(ctx, "TESTWP-50", account, minutes_ago=minutes)
    for node in ("cc", "node1"):
        ctx.db.run("INSERT INTO nodes (name,role,status,slots_total,last_heartbeat,projects,dynamic,accepting_work)"
                   " VALUES (%s,'worker','online',2,now(),%s,false,true) ON CONFLICT (name) DO UPDATE SET"
                   " status='online',last_heartbeat=now(),projects=EXCLUDED.projects,dynamic=false,accepting_work=true",
                   (node, ["TESTWP"]))
    for account, provider, node in (("claude-01", "claude", "cc"), ("codex-01", "codex", "node1")):
        ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status) VALUES (%s,%s,%s,'middle',%s,'idle')"
                   " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,node=EXCLUDED.node,"
                   "roles=EXCLUDED.roles,status='idle'", (account, provider, node, ["dev", "verify", "master"]))

    loops._check_verify_incidents(ctx, tmp_path)

    assert ctx.candidate_offers is not None
    assert ctx.db.one("SELECT subject FROM incidents WHERE kind='verify_no_verdict' AND closed_at IS NULL") == {
        "subject": "TESTWP-50"}


def test_watchdog_uses_the_capped_candidate_path_for_exhaustion(client, ctx, monkeypatch):
    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    for minutes in (8, 2):
        no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=minutes)

    from aiflow.v4.control import work_candidates

    def ordered_candidates(state_):
        fillers = [{"key": f"FILLER-{index}", "project": "TESTWP", "step": "verify"} for index in range(20)]
        target = {"key": "TESTWP-50", "project": "TESTWP", "step": "verify"}
        return [*fillers, target] if state_.account.id == "claude-01" else [target, *fillers]

    monkeypatch.setattr(work_candidates, "collect", ordered_candidates)
    ticket = {"key": "TESTWP-50", "project": "TESTWP", "labels": ["aiflow-ready"]}

    assert len(ctx.candidate_offers("claude-01", "cc")) == 20
    assert "TESTWP-50" not in {item["key"] for item in ctx.candidate_offers("claude-01", "cc")}
    assert verify_retry.pool(ctx, ticket, "opencode-01") == ["codex-01"]
    assert verify_retry.exhausted(ctx, ticket) == ["codex-01"]

    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                      " AND subject='TESTWP-50' AND closed_at IS NULL")["n"] == 1


def test_unrelated_watchdog_check_failure_does_not_skip_verify_exhaustion(client, ctx, monkeypatch):
    import aiflow.v4.control.loops as loops

    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        for minutes in (8, 2):
            no_verdict(ctx, "TESTWP-50", account, minutes_ago=minutes)

    def fail_idle_check(_ctx):
        raise RuntimeError("injected unrelated watchdog failure")

    monkeypatch.setattr(loops, "check_idle_flow", fail_idle_check)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                      " AND subject='TESTWP-50' AND closed_at IS NULL")["n"] == 1


def test_exhaustion_incident_stays_open_across_offline_and_return_cycle(client, ctx):
    cc, node1 = join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        for minutes in (8, 2):
            no_verdict(ctx, "TESTWP-50", account, minutes_ago=minutes)

    watchdog(ctx)
    original = ctx.db.one("SELECT id FROM incidents WHERE kind='verify_no_verdict' AND subject='TESTWP-50'"
                          " AND closed_at IS NULL")
    assert original

    ctx.db.run("UPDATE nodes SET last_heartbeat=now()-interval '2 minutes' WHERE name IN ('cc','node1')")
    assert verify_retry.pool(ctx, {"key": "TESTWP-50", "project": "TESTWP", "labels": ["aiflow-ready"]},
                             "opencode-01") == []
    watchdog(ctx)
    still_open = ctx.db.one("SELECT id FROM incidents WHERE kind='verify_no_verdict' AND subject='TESTWP-50'"
                            " AND closed_at IS NULL")
    assert still_open == original

    for headers, node in ((cc, "cc"), (node1, "node1")):
        heartbeat = client.post(f"/v1/nodes/{node}/heartbeat", json={"slots_total": 2}, headers=headers)
        assert heartbeat.status_code == 200
    watchdog(ctx)
    returned = ctx.db.one("SELECT id FROM incidents WHERE kind='verify_no_verdict' AND subject='TESTWP-50'"
                          " AND closed_at IS NULL")
    total = ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                       " AND subject='TESTWP-50'")["n"]
    assert returned == original and total == 1

    insert_run(ctx, "TESTWP-50", "verify", "claude-01", "rework", {"verdict": "FAIL"}, minutes_ago=0)
    watchdog(ctx)
    assert ctx.db.one("SELECT closed_at FROM incidents WHERE id=%s", (original["id"],))["closed_at"] is not None


def test_dashboard_lists_only_tickets_waiting_longer_than_30_minutes(ctx):
    in_verify(ctx, "TESTWP-80")
    add_ticket(ctx.db, "TESTWP-81", "In Verify")
    insert_run(ctx, "TESTWP-81", "dev", "opencode-01", "done", pr_url=PR, minutes_ago=60)
    checked_at = ctx.db.one("SELECT now() AS t")["t"]
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '31 minutes' WHERE ticket_key='TESTWP-80' AND step='dev'",
               (checked_at,))
    ctx.db.run("UPDATE runs SET ended_at=%s-interval '30 minutes' WHERE ticket_key='TESTWP-81' AND step='dev'",
               (checked_at,))
    assert [v["key"] for v in verify_retry.slow_verifications(ctx.db, now=checked_at)] == ["TESTWP-80"]


def test_dashboard_describes_tried_account_without_no_verdict_details(ctx):
    in_verify(ctx, "TESTWP-82")
    insert_run(ctx, "TESTWP-82", "verify", "claude-01", "quota", {"no_verdict": False}, minutes_ago=5)

    slow = verify_retry.slow_verifications(ctx.db)[0]
    described = verify_retry.describe(slow)

    assert "claude-01" in described
    assert "no verdict" not in described
    assert "no account has tried yet" not in described


def test_owner_dashboard_shows_account_ids_for_slow_verifications(client, ctx):
    in_verify(ctx, "TESTWP-83")
    no_verdict(ctx, "TESTWP-83", "claude-01", minutes_ago=5)

    data = client.get("/v1/state").json()

    assert data["slow_verifications"][0]["key"] == "TESTWP-83"
    assert data["slow_verifications"][0]["accounts"][0]["account"] == "claude-01"
    ticket = next(row for row in data["tickets"] if row["key"] == "TESTWP-83")
    assert "claude-01" in ticket["slow_verify"]


def test_untried_account_that_cannot_receive_the_ticket_does_not_hold_the_retry_back(client, ctx):
    cc = join(client, "cc")
    join(client, "node1")
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "claude-01")
    assert ("TESTWP-50", "verify") not in offered(client, cc, "claude-01")  # codex-01 is online and untried
    # node1 stays online but stops accepting work: codex-01 cannot take the ticket, so claude-01 gets the retry
    ctx.db.run("UPDATE nodes SET accepting_work=false WHERE name='node1'")
    assert ("TESTWP-50", "verify") in offered(client, cc, "claude-01")


def test_candidate_refused_after_eligible_does_not_hold_the_retry_back(client, ctx, monkeypatch):
    from aiflow.v4.control import licensing
    cc = join(client, "cc")
    join(client, "node1")
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "claude-01")
    assert ("TESTWP-50", "verify") not in offered(client, cc, "claude-01")  # codex-01 is untried and offered
    real = licensing.enforce_work_entitlement

    def refuse_codex(ctx_, account, node, **kwargs):
        if account == "codex-01":
            raise licensing.LicenseError("plan limit")
        return real(ctx_, account, node, **kwargs)

    monkeypatch.setattr(licensing, "enforce_work_entitlement", refuse_codex)
    # Codex's ordinary candidate path is entitlement-blocked, so it cannot hold the retry back.
    assert ("TESTWP-50", "verify") in offered(client, cc, "claude-01")


def test_offline_account_does_not_keep_the_ticket_from_being_exhausted(client, ctx):
    join(client, "cc")  # codex-01's machine never joins: it is offline
    in_verify(ctx)
    for minutes in (8, 2):
        no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=minutes)
    assert verify_retry.exhausted(ctx, {"key": "TESTWP-50", "project": "TESTWP", "status": "In Verify",
                                       "labels": ["aiflow-ready"]}) == ["claude-01"]
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict'"
                      " AND closed_at IS NULL")["n"] == 1


def test_master_paused_account_does_not_delay_verify_exhaustion(client, ctx):
    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    ctx.db.run("INSERT INTO master_account_pauses (account_id, paused_until, reason, run_id)"
               " VALUES ('claude-01', now() + interval '1 hour', 'master pause', %s)", (str(uuid.uuid4()),))
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=8)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=2)

    assert verify_retry.exhausted(ctx, {"key": "TESTWP-50", "project": "TESTWP", "labels": ["aiflow-ready"]}) == [
        "codex-01"]
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict' AND closed_at IS NULL")["n"] == 1


def test_account_without_shared_capacity_does_not_delay_verify_exhaustion(client, ctx, monkeypatch):
    from aiflow.v4.control import shared_accounts

    join(client, "cc"), join(client, "node1")
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=8)
    no_verdict(ctx, "TESTWP-50", "codex-01", minutes_ago=2)
    normal_capacity = shared_accounts.may_take

    def full_account(db, account, node, **kwargs):
        if account.id == "claude-01":
            return False, "account capacity is full"
        return normal_capacity(db, account, node, **kwargs)

    monkeypatch.setattr(shared_accounts, "may_take", full_account)

    assert verify_retry.exhausted(ctx, {"key": "TESTWP-50", "project": "TESTWP", "labels": ["aiflow-ready"]}) == [
        "codex-01"]
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='verify_no_verdict' AND closed_at IS NULL")["n"] == 1


def test_online_account_without_project_access_does_not_prevent_exhaustion(client, ctx):
    join(client, "cc")
    join(client, "node1")
    join(client, "node2")
    ctx.db.run("UPDATE nodes SET projects=%s,dynamic=false WHERE name='node2'", ([],))
    ctx.repo().accounts.append(Account(id="grok-01", provider="grok", node="node2", roles=["verify"]))
    in_verify(ctx)
    for account in ("codex-01", "claude-01"):
        for minutes in (8, 2):
            no_verdict(ctx, "TESTWP-50", account, minutes_ago=minutes)

    assert verify_retry.exhausted(ctx, {"key": "TESTWP-50", "project": "TESTWP", "labels": ["aiflow-ready"]}) == [
        "claude-01", "codex-01"]


def test_concurrent_candidate_checks_keep_retry_probe_request_local(client, ctx, monkeypatch):
    cc, node1 = join(client, "cc"), join(client, "node1")
    ctx.repo().accounts.append(Account(id="grok-01", provider="grok", node="cc", roles=["verify"]))
    in_verify(ctx)
    no_verdict(ctx, "TESTWP-50", "codex-01")
    no_verdict(ctx, "TESTWP-50", "claude-01", minutes_ago=4)
    entered_probe, release_probe = Event(), Event()

    from aiflow.v4.control import licensing
    real_enforce = licensing.enforce_work_entitlement

    def pause_probe(ctx_, account, node, **kwargs):
        if account == "grok-01":
            entered_probe.set()
            assert release_probe.wait(5)
        return real_enforce(ctx_, account, node, **kwargs)

    monkeypatch.setattr(licensing, "enforce_work_entitlement", pause_probe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get, "/v1/work/candidates", params={"account": "codex-01"}, headers=node1)
        assert entered_probe.wait(3)
        second = pool.submit(client.get, "/v1/work/candidates", params={"account": "claude-01"}, headers=cc)
        try:
            time.sleep(0.15)
            assert not second.done()
        finally:
            release_probe.set()
        first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert ("TESTWP-50", "verify") not in {
        (ticket["key"], ticket["step"]) for ticket in second_result.json()["tickets"]}
