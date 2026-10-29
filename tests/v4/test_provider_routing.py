"""Provider routing by quota (JAIF-264): headroom order, ceiling, return after reset, unknown quota, role coverage."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import add_open_escalation, add_ticket, join_node

from aiflow.v4.control import capacity, licensing, provider_routing, shared_accounts
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.db import J
from aiflow.v4.control.loops import watchdog
from aiflow.v4.control.repositories.accounts import AccountRepository
from aiflow.v4.control.repositories.provider_routing import ProviderRoutingRepository
from aiflow.v4.core.controlrepo import Account

SESSION_SECRET = "test-console-session-secret-at-least-32-bytes"


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['node_token']}"}
    assert client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2}, headers=headers).status_code == 200
    return headers


def fleet(client, ctx):
    """claude-01 on cc and codex-01 on node1, both online and ready."""
    heads = {"cc": join(client, "cc"), "node1": join(client, "node1")}
    ctx.db.run("UPDATE accounts SET status='idle'")
    return heads


def sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SESSION_SECRET
    client.cookies.set("aiflow4_session", issue_session("owner@example.test", role, 2_000_000_000, SESSION_SECRET))


def set_quota(ctx, account, **windows):
    """windows: name -> (used percent, reset time or None)"""
    quota = {"windows": {name: {"utilization": used / 100, **({"resets_at": reset.isoformat()} if reset else {})}
                         for name, (used, reset) in windows.items()}}
    ctx.db.run("UPDATE accounts SET quota=%s::jsonb WHERE id=%s", (json.dumps(quota), account))


def set_capacity_forecast(ctx, account, remaining_tokens):
    forecast = {"source": "reported", "confidence": "high", "windows": [
        {"name": "five_hour", "left_tokens": remaining_tokens, "prediction": {}}]}
    ctx.db.run("UPDATE accounts SET quota_forecast=%s::jsonb WHERE id=%s",
               (json.dumps(forecast), account))


def offered(client, heads, account, node):
    client.app.state.ctx.provider_eligibility.reset_cache()  # headroom is shared between polls; read the current state
    return [t["key"] for t in client.get("/v1/work/candidates", params={"account": account},
                                         headers=heads[node]).json()["tickets"]]


def hours(n):
    return datetime.now(timezone.utc) + timedelta(hours=n)


def state(ctx):
    return evaluate_routing(ctx, ["claude", "codex"], persist=False)["providers"]


def configure_expired_limit(ctx, resource, limit):
    plans = {name: dict(values) for name, values in licensing.PLAN_DEFAULTS.items()}
    plans["free"][resource] = limit
    ctx.settings.license_public_key = "configured-test-issuer"
    ctx.db.run("UPDATE licensing_state SET plan_limits=%s, over_limit_since=now() - interval '15 days' WHERE id=1",
               (J(plans),))


def evaluate_routing(ctx, providers, **options):
    accounts = AccountRepository(ctx.db).quota_routing_rows(providers)
    return provider_routing.evaluate(ProviderRoutingRepository(ctx.db), providers, accounts, **options)


def test_default_order_prefers_codex_when_providers_are_otherwise_equal(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-1")
    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-1"]
    assert offered(client, heads, "claude-01", "cc") == []  # codex can take it, so claude waits
    assert evaluate_routing(ctx, ["claude", "codex", "grok"], persist=False)["ranking"] == [
        "codex", "grok", "claude"]


def test_provider_with_most_headroom_is_offered_first(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-2")
    set_quota(ctx, "codex-01", five_hour=(70, hours(2)))   # 30% left
    set_quota(ctx, "claude-01", five_hour=(10, hours(2)))  # 90% left
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-2"]
    assert offered(client, heads, "codex-01", "node1") == []
    states = state(ctx)
    assert states["claude"]["preference"] == "spend first" and states["codex"]["preference"] == "keep for need"
    assert states["claude"]["headroom_pct"] == 90.0


def test_near_tie_headroom_uses_full_score_before_saved_order(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-22")
    set_quota(ctx, "codex-01", five_hour=(20, hours(2)))   # 80 points of headroom
    set_quota(ctx, "claude-01", five_hour=(11, hours(2)))  # 89 points of headroom
    assert state(ctx)["claude"]["score"] > state(ctx)["codex"]["score"]
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-22"]
    assert offered(client, heads, "codex-01", "node1") == []  # default order breaks equal scores only


def test_imminent_quota_reset_improves_spend_preference(client, ctx):
    fleet(client, ctx)
    now = datetime.now(timezone.utc)
    set_quota(ctx, "codex-01", five_hour=(30, now + timedelta(hours=2)))
    set_quota(ctx, "claude-01", five_hour=(30, now + timedelta(minutes=30)))
    routing = evaluate_routing(ctx, ["codex", "claude"], now=now, persist=False)
    assert routing["providers"]["claude"]["score"] == routing["providers"]["codex"]["score"] + 10
    assert routing["ranking"] == ["claude", "codex"]


def test_forecast_to_run_out_lowers_the_preference(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "codex-01", five_hour=(40, hours(3)))
    set_quota(ctx, "claude-01", five_hour=(40, hours(3)))
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='codex-01'")
    states = state(ctx)
    assert states["codex"]["score"] < states["claude"]["score"]
    assert states["claude"]["preference"] == "spend first"


def test_saved_owner_order_is_used_for_new_offers(client, ctx):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-3")
    saved = client.put("/v1/provider-routing/settings", json={"order": ["claude", "codex"], "ceilings": {}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["order"][:2] == ["claude", "codex"]
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-3"]
    assert offered(client, heads, "codex-01", "node1") == []
    assert [p["provider"] for p in client.get("/v1/provider-routing").json()["providers"]][:2] == ["claude", "codex"]


def test_only_an_owner_changes_the_settings_and_values_are_checked(client, ctx):
    fleet(client, ctx)
    sign_in(client, ctx, role="reviewer")
    denied = client.put("/v1/provider-routing/settings", json={"order": [], "ceilings": {"claude": 80}})
    assert denied.status_code == 403
    assert provider_routing.get_settings(ProviderRoutingRepository(ctx.db))["ceilings"] == {}
    sign_in(client, ctx, role="owner")
    assert client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 0}}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 101}}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"order": ["nobody"]}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"order": "claude"}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": "high"}}).status_code == 422
    duplicate = client.put("/v1/provider-routing/settings", json={
        "order": ["claude", "claude", "codex"], "ceilings": {"claude": 1, "codex": 100}})
    assert duplicate.status_code == 200 and duplicate.json()["order"][:2] == ["claude", "codex"]
    assert duplicate.json()["settings"]["ceilings"] == {"claude": 1, "codex": 100}
    empty = client.put("/v1/provider-routing/settings", json={"order": [], "ceilings": {}})
    assert empty.status_code == 200 and empty.json()["order"][:2] == ["codex", "claude"]
    ok = client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 80}})
    assert ok.status_code == 200 and ok.json()["settings"]["ceilings"] == {"claude": 80}


def test_provider_above_its_ceiling_is_held_back_in_any_window_but_keeps_label_and_triage(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-4")
    add_ticket(ctx.db, "TESTWP-5", labels=("aiflow-ready", "ai-claude"))
    # codex has no free headroom either way: claude is the only provider that could be preferred for TESTWP-4
    set_quota(ctx, "claude-01", five_hour=(40, hours(2)), seven_day=(90, hours(60)))
    held = state(ctx)["claude"]
    assert held["held_back"] and "seven day" in held["held_reason"] and held["ceiling_pct"] == 85
    assert held["preference"] == "held back"
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-5"]  # only the ticket labelled for it
    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-4", "TESTWP-5"]


def test_held_back_provider_keeps_triage_work(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-6", status="Needs owner", labels=())
    add_open_escalation(ctx.db, "TESTWP-6")
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    tickets = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=heads["cc"]).json()["tickets"]
    assert [(t["key"], t["step"]) for t in tickets] == [("TESTWP-6", "triage")]


def test_held_back_provider_keeps_master_cycle_available(client, ctx):
    heads = fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    assert state(ctx)["claude"]["held_back"]
    candidate = client.get("/v1/master/candidate", params={"account": "claude-01"},
                           headers=heads["cc"]).json()["candidate"]
    assert candidate and candidate["step"] == "master"


def test_owner_ceiling_per_provider_changes_what_is_held(client, ctx):
    fleet(client, ctx)
    sign_in(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(80, hours(2)))
    assert not state(ctx)["claude"]["held_back"]  # default ceiling is 85%
    client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 75}})
    assert state(ctx)["claude"]["held_back"] and state(ctx)["claude"]["ceiling_pct"] == 75
    client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 95}})
    set_quota(ctx, "claude-01", five_hour=(90, hours(2)))
    assert not state(ctx)["claude"]["held_back"]


def test_held_back_provider_returns_below_sixty_percent_not_before(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(90, hours(2)))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(70, hours(2)))  # under the ceiling but not under 60%
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(55, hours(2)))
    back = evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]
    assert not back["held_back"]
    assert ctx.db.one("SELECT count(*) AS n FROM provider_routing_state")["n"] == 0


@pytest.mark.parametrize("update", [
    {"order": ["claude", "codex"]},
    {"ceilings": {"codex": 80}},
])
def test_settings_updates_preserve_hold_until_reset_or_sixty_percent(client, ctx, update):
    fleet(client, ctx)
    sign_in(client, ctx)
    initial = client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 75}})
    assert initial.status_code == 200, initial.text
    set_quota(ctx, "claude-01", five_hour=(90, hours(2)))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]

    set_quota(ctx, "claude-01", five_hour=(70, hours(2)))
    saved = client.put("/v1/provider-routing/settings", json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"]["ceilings"]["claude"] == 75
    assert saved.json()["settings"]["ceilings"].get("codex", 85) == (80 if "ceilings" in update else 85)
    assert state(ctx)["claude"]["held_back"]

    set_quota(ctx, "claude-01", five_hour=(59.96, hours(2)))
    assert not state(ctx)["claude"]["held_back"]


def test_ceiling_uses_full_precision_just_above_eighty_five_percent(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(85, hours(2)))
    assert not state(ctx)["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(85.01, hours(2)))
    claude = state(ctx)["claude"]
    assert claude["held_back"]
    assert claude["windows"][0]["used_pct"] == 85.01


def test_held_provider_releases_at_full_precision_just_below_sixty_percent(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(90, hours(2)))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(60, hours(2)))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(59.96, hours(2)))
    claude = evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]
    assert not claude["held_back"]
    assert claude["windows"][0]["used_pct"] == 59.96


def test_held_back_provider_returns_when_its_window_resets(client, ctx):
    fleet(client, ctx)
    reset = datetime.now(timezone.utc) + timedelta(minutes=30)
    set_quota(ctx, "claude-01", five_hour=(92, reset))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    # the window's own reset time passes while the last sample still reports 92%
    later = reset + timedelta(minutes=1)
    after = evaluate_routing(ctx, ["claude", "codex"], now=later)["providers"]["claude"]
    assert not after["held_back"] and after["windows"][0]["reset"] is True
    # a fresh sample for the next window counts again
    set_quota(ctx, "claude-01", five_hour=(20, later + timedelta(hours=5)))
    assert not evaluate_routing(ctx, ["claude", "codex"], now=later)["providers"]["claude"]["held_back"]


def test_each_window_uses_its_own_reset_time(client, ctx):
    fleet(client, ctx)
    now = datetime.now(timezone.utc)
    set_quota(ctx, "claude-01", five_hour=(95, now + timedelta(hours=1)), seven_day=(30, now + timedelta(days=3)))
    assert evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]["held_back"]
    # the five hour window resets; the seven day window (30%) does not hold the provider back
    after = evaluate_routing(ctx, ["claude", "codex"], now=now + timedelta(hours=2))["providers"]["claude"]
    assert not after["held_back"]
    assert {w["name"]: w["reset"] for w in after["windows"]} == {"five_hour": True, "seven_day": False}


def test_one_account_above_the_ceiling_is_not_averaged_with_another(client, ctx):
    fleet(client, ctx)
    reset = hours(1)
    ctx.db.run("INSERT INTO accounts (id,provider,node,status) VALUES ('claude-02','claude','cc','idle')")
    set_quota(ctx, "claude-01", five_hour=(95, reset))
    set_quota(ctx, "claude-02", five_hour=(0, hours(5)))
    held = evaluate_routing(ctx, ["claude", "codex"])["providers"]["claude"]
    assert held["held_back"]
    assert "claude-01" in held["held_reason"]
    high_window = next(window for window in held["windows"] if window["account"] == "claude-01")
    assert high_window["reset_at"] == reset.isoformat()

    after = evaluate_routing(ctx, ["claude", "codex"], now=reset + timedelta(minutes=1))["providers"]["claude"]
    assert not after["held_back"]  # the specific over-limit account window reset


def test_provider_without_quota_is_treated_as_having_headroom_and_is_marked_unknown(client, ctx):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-7")
    set_quota(ctx, "claude-01", five_hour=(50, hours(2)))  # codex reports nothing
    codex = state(ctx)["codex"]
    assert codex["unknown"] is True and codex["headroom_pct"] == 100.0 and not codex["held_back"]
    assert "unknown" in codex["note"].lower()
    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-7"]
    view = client.get("/v1/provider-routing").json()
    assert next(p for p in view["providers"] if p["provider"] == "codex")["unknown"] is True


def test_every_claim_records_why_it_went_to_the_account(client, ctx):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-8")
    add_ticket(ctx.db, "TESTWP-9", labels=("aiflow-ready", "ai-claude"))
    first = client.post("/v1/work/claim", json={"ticket": "TESTWP-8", "account": "codex-01"}, headers=heads["node1"])
    assert first.status_code == 200, first.text
    labelled = client.post("/v1/work/claim", json={"ticket": "TESTWP-9", "account": "claude-01"}, headers=heads["cc"])
    assert labelled.status_code == 200, labelled.text
    decisions = {d["ticket"]: d for d in client.get("/v1/provider-routing").json()["decisions"]}
    assert decisions["TESTWP-8"]["kind"] == "headroom" and "codex first" in decisions["TESTWP-8"]["reason"]
    assert decisions["TESTWP-9"]["kind"] == "label" and "ai-claude" in decisions["TESTWP-9"]["reason"]


def test_held_back_provider_stays_a_fallback_when_no_other_provider_can_take_the_work(client, ctx):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-10")
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    assert offered(client, heads, "claude-01", "cc") == []  # codex could take it
    ctx.db.run("UPDATE accounts SET status='signed_out' WHERE id='codex-01'")  # nobody else can do it now
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-10"]
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-10", "account": "claude-01"}, headers=heads["cc"])
    assert claim.status_code == 200
    decision = client.get("/v1/provider-routing").json()["decisions"][0]
    assert decision["kind"] == "fallback" and "held back" in decision["reason"]


def test_held_back_provider_falls_back_when_preferred_peer_is_full_but_online(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-11")
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    ctx.db.run("UPDATE nodes SET slots_total=1 WHERE name='node1'")
    ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,started_at)"
               " VALUES (%s,'BUSY-1','TESTWP','dev','codex-01','codex','node1',1,'running',now())",
               (str(uuid.uuid4()),))

    peer = ctx.repo().account("codex-01")
    assert peer is not None and peer.session_limit == 0
    assert "node1" in shared_accounts.online_nodes(ctx.db)
    assert not shared_accounts.may_take(ctx.db, peer, "node1", as_peer=True)[0]
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-11"]


@pytest.mark.parametrize(("resource", "limit"), [("ai_accounts", 1), ("machines", 1)])
def test_held_back_provider_falls_back_when_preferred_peer_exceeds_license_limit(client, ctx, resource, limit):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    configure_expired_limit(ctx, resource, limit)
    ctx.db.run("UPDATE accounts SET status='signed_out' WHERE id='opencode-01'")
    add_ticket(ctx.db, "TESTWP-14")
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    saved = client.put("/v1/provider-routing/settings", json={"order": ["codex", "claude"]})
    assert saved.status_code == 200, saved.text

    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-14"]
    peer_response = client.get("/v1/work/candidates", params={"account": "codex-01"},
                               headers=heads["node1"]).json()
    assert peer_response["tickets"] == []
    assert resource.replace("_", " ")[:-1].lower() in peer_response["reason"].lower()


def test_held_back_provider_is_not_suppressed_by_peer_rejected_by_quota_capacity(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-12")
    set_quota(ctx, "codex-01", five_hour=(10, hours(2)))
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    set_capacity_forecast(ctx, "codex-01", 0)
    set_capacity_forecast(ctx, "claude-01", 1_000_000)

    assert state(ctx)["claude"]["held_back"]
    assert offered(client, heads, "codex-01", "node1") == []
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-12"]


def test_incompatible_online_peer_does_not_suppress_a_compatible_provider_offer(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-23")
    ctx.settings.control_version = "v4.1.0"
    ctx.db.run("UPDATE nodes SET release_version='v9.0.0' WHERE name='node1'")

    peer_candidates = client.get("/v1/work/candidates", params={"account": "codex-01"},
                                 headers=heads["node1"]).json()
    assert peer_candidates["tickets"] == []
    assert "version skew" in peer_candidates["reason"]
    rejected_claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-23", "account": "codex-01"},
                                 headers=heads["node1"])
    assert rejected_claim.status_code == 409 and "version skew" in rejected_claim.json()["reason"]

    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-23"]


def test_capacity_routing_keeps_headroom_provider_when_held_back_peer_has_more_tokens(client, ctx):
    heads = fleet(client, ctx)
    sign_in(client, ctx)
    add_ticket(ctx.db, "TESTWP-24", priority="Highest")
    add_ticket(ctx.db, "TESTWP-25", labels=("aiflow-ready", "ai-claude"), priority="Highest")
    set_quota(ctx, "codex-01", five_hour=(10, hours(2)))
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    set_quota(ctx, "opencode-01", five_hour=(95, hours(2)))
    set_capacity_forecast(ctx, "codex-01", 25_000)
    set_capacity_forecast(ctx, "claude-01", 250_000)

    ticket = ctx.db.one("SELECT * FROM tickets WHERE key='TESTWP-24'")
    estimate = capacity.estimate(ticket, capacity.history(ctx.db))
    rows = {row["id"]: row for row in AccountRepository(ctx.db).capacity_routing_rows()}
    candidates = [rows["codex-01"], rows["claude-01"]]
    raw_choice, fits = capacity.choose_account(
        candidates, ticket, estimate, {account["id"]: account["quota_forecast"] for account in candidates})
    assert raw_choice == "claude-01"  # both fit; its senior tier and larger forecast tokens win capacity alone
    assert all(fits[account["id"]]["known"] and fits[account["id"]]["fits"] for account in candidates)
    assert state(ctx)["claude"]["held_back"]

    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-24", "TESTWP-25"]
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-25"]
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-24", "account": "codex-01"},
                        headers=heads["node1"])
    assert claim.status_code == 200, claim.text
    decision = next(item for item in client.get("/v1/provider-routing").json()["decisions"]
                    if item["ticket"] == "TESTWP-24")
    assert decision["kind"] == "headroom" and "codex first" in decision["reason"]


def test_quota_risk_peer_probe_respects_quota_capacity(client, ctx):
    from aiflow.v4.core.controlrepo import Account

    heads = fleet(client, ctx)
    ctx.repo().accounts.append(Account(id="codex-02", provider="codex", node="node1", roles=["dev"]))
    client.app.state.sync_accounts()
    ctx.db.run("UPDATE accounts SET status='idle',quota_at_risk=false WHERE id='codex-02'")
    add_ticket(ctx.db, "TESTWP-13")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='codex-01'")
    set_capacity_forecast(ctx, "codex-01", 1_000_000)
    set_capacity_forecast(ctx, "codex-02", 0)

    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-13"]
    assert offered(client, heads, "codex-02", "node1") == []


def test_quota_risk_master_probe_ignores_incompatible_peer(client, ctx):
    from aiflow.v4.core.controlrepo import Account

    heads = fleet(client, ctx)
    heads["node2"] = join(client, "node2")
    ctx.settings.control_version = "v4.1.0"
    ctx.db.run("UPDATE nodes SET release_version='v9.0.0' WHERE name='node1'")
    repo = ctx.repo()
    repo.account("codex-01").extra_machines.append("node2")
    repo.accounts.append(Account(id="codex-02", provider="codex", node="node1", roles=["master"]))
    ctx.repo = lambda: repo
    ctx.db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
               " VALUES ('codex-02','codex','node1','middle',ARRAY['master'],'idle')")
    ctx.db.run("UPDATE accounts SET status='idle',quota_at_risk=false WHERE id IN ('codex-01','codex-02')")
    ctx.db.run("UPDATE accounts SET quota_at_risk=true WHERE id='codex-01'")

    candidate = client.get("/v1/master/candidate", params={"account": "codex-01"},
                           headers=heads["node2"]).json()["candidate"]
    assert candidate and candidate["step"] == "master"


def test_held_back_provider_does_not_break_role_coverage(client, ctx):
    fleet(client, ctx)
    repo = ctx.repo()
    repo.account("claude-01").roles.append("release")
    repo.account("codex-01").roles.append("release")
    ctx.repo = lambda: repo
    set_quota(ctx, "claude-01", five_hour=(97, hours(2)))
    watchdog(ctx)
    assert state(ctx)["claude"]["held_back"]
    # Held-back accounts remain a fallback for all work roles, including post_merge release.
    for role in ("dev", "verify", "master", "release"):
        assert not ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_coverage' AND subject=%s"
                              " AND closed_at IS NULL", (role,))
    assert ctx.db.one("SELECT status FROM accounts WHERE id='claude-01'")["status"] in ("idle", "working", "probing")


def test_role_coverage_excludes_paused_accounts_from_release_readiness(client, ctx):
    fleet(client, ctx)
    repo = ctx.repo()
    repo.account("claude-01").roles = ["release"]
    repo.account("claude-01").paused = True
    repo.account("codex-01").roles = ["release"]
    repo.account("opencode-01").roles = []
    ctx.repo = lambda: repo

    watchdog(ctx)

    incident = ctx.db.one("SELECT title,detail FROM incidents WHERE kind='role_coverage' AND subject='release'"
                          " AND closed_at IS NULL")
    assert incident is not None
    assert "1 ready provider" in incident["title"]
    assert "ready: codex-01 across codex" in incident["detail"]
    assert "claude-01" not in incident["detail"].split("ready:", 1)[1]


def test_role_coverage_rejects_two_ready_accounts_from_the_same_provider(client, ctx):
    fleet(client, ctx)
    repo = ctx.repo()
    repo.account("claude-01").roles = ["verify", "master"]
    repo.account("opencode-01").roles = []
    repo.accounts.append(Account(id="codex-02", provider="codex", node="node1", roles=["dev"]))
    client.app.state.sync_accounts()
    ctx.db.run("UPDATE accounts SET status='idle'")

    watchdog(ctx)

    incident = ctx.db.one("SELECT title,detail FROM incidents WHERE kind='role_coverage' AND subject='dev'"
                          " AND closed_at IS NULL")
    assert incident is not None
    assert "1 ready provider" in incident["title"]
    assert "codex-01, codex-02" in incident["detail"]
    assert "across codex" in incident["detail"]


def test_quota_risk_and_headroom_are_registered_eligibility_rules(client, ctx):
    names = ctx.provider_eligibility.pipeline.rule_names
    assert names == ("quota_forecast_risk", "provider_headroom")


def test_all_providers_held_back_use_highest_ranked_eligible_provider_first(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-20")
    set_quota(ctx, "claude-01", five_hour=(97, hours(2)))
    set_quota(ctx, "codex-01", five_hour=(90, hours(2)))
    held = state(ctx)
    assert held["claude"]["held_back"] and held["codex"]["held_back"] and held["codex"]["rank"] == 0
    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-20"]
    assert offered(client, heads, "claude-01", "cc") == []  # a lower-ranked held-back provider waits
    ctx.db.run("UPDATE accounts SET status='signed_out' WHERE id='codex-01'")
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-20"]  # fallback keeps the work moving


def test_usage_exactly_at_the_ceiling_is_not_held_back_but_above_it_is(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(85, hours(2)))
    assert not state(ctx)["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(85.5, hours(2)))
    assert state(ctx)["claude"]["held_back"]


def test_held_back_provider_yields_to_a_provider_that_is_not_held_back_whatever_the_order(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-21")
    set_quota(ctx, "codex-01", five_hour=(95, hours(2)))
    set_quota(ctx, "claude-01", five_hour=(10, hours(2)))
    assert state(ctx)["claude"]["rank"] == 0
    assert offered(client, heads, "codex-01", "node1") == []
    assert offered(client, heads, "claude-01", "cc") == ["TESTWP-21"]


def test_routing_explanations_stay_available_per_ticket_beyond_thirty_claims(client, ctx):
    fleet(client, ctx)
    sign_in(client, ctx)
    for n in range(40):
        ctx.db.run("INSERT INTO routing_decisions (ticket_key, step, account_id, provider, kind, reason)"
                   " VALUES (%s,'dev','codex-01','codex','headroom',%s)", (f"TESTWP-{100 + n}", f"reason {n}"))
    assert len(client.get("/v1/provider-routing").json()["decisions"]) == 30
    one = client.get("/v1/provider-routing/decisions", params={"ticket": "testwp-100"}).json()
    assert one["total"] == 1 and one["decisions"][0]["reason"] == "reason 0"
    page = client.get("/v1/provider-routing/decisions", params={"limit": 30, "offset": 30}).json()
    assert page["total"] == 40 and len(page["decisions"]) == 10
    assert "TESTWP-100" in {d["ticket"] for d in page["decisions"]}


def test_machines_screen_has_the_routing_panel():
    from pathlib import Path
    root = Path(__file__).parents[2] / "aiflow/v4/control/ui"
    html = (root / "console.html").read_text()
    ui = (root / "__init__.py").read_text()
    feature = (root / "provider-routing.js").read_text()
    assert 'src="/console-assets/licensing-notice.js"' in html
    assert "A role the flow needs (master, intake, verify, dev, release)" in html
    assert 'console-features.js' in ui
    assert "MutationObserver" in feature
    for needle in ("AI provider routing", "Spend preference", "Preferred order",
                   'data-routing-action="save"', "data-routing-ceiling",
                   "Why each ticket went to its account", "Quota unknown", "/v1/provider-routing/settings",
                   "percent(item.used_pct)"):
        assert needle in feature


def test_a_held_provider_keeps_its_hold_when_the_quota_response_has_no_window():
    from datetime import datetime, timedelta, timezone

    from aiflow.v4.control import provider_routing

    now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    held_row = {"window_name": "five_hour", "window_reset_at": now + timedelta(hours=2)}
    held, reason = provider_routing._previous_hold(held_row, [], 60, now)
    assert held and "no quota window is reported" in reason
    released, _ = provider_routing._previous_hold({**held_row, "window_reset_at": now - timedelta(minutes=1)}, [], 60, now)
    assert released is False  # its window has reset
