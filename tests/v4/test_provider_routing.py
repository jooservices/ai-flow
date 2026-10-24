"""Provider routing by quota (JAIF-264): headroom order, ceiling, return after reset, unknown quota, role coverage."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import add_ticket, join_node

from aiflow.v4.control import provider_routing
from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.loops import watchdog

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


def offered(client, heads, account, node):
    return [t["key"] for t in client.get("/v1/work/candidates", params={"account": account},
                                         headers=heads[node]).json()["tickets"]]


def hours(n):
    return datetime.now(timezone.utc) + timedelta(hours=n)


def state(ctx):
    return provider_routing.evaluate(ctx.db, ["claude", "codex"], persist=False)["providers"]


def test_default_order_prefers_codex_when_providers_are_otherwise_equal(client, ctx):
    heads = fleet(client, ctx)
    add_ticket(ctx.db, "TESTWP-1")
    assert offered(client, heads, "codex-01", "node1") == ["TESTWP-1"]
    assert offered(client, heads, "claude-01", "cc") == []  # codex can take it, so claude waits
    assert provider_routing.evaluate(ctx.db, ["claude", "codex", "grok"], persist=False)["ranking"] == [
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
    assert provider_routing.get_settings(ctx.db)["ceilings"] == {}
    sign_in(client, ctx, role="owner")
    assert client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 0}}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"ceilings": {"claude": 101}}).status_code == 422
    assert client.put("/v1/provider-routing/settings", json={"order": ["nobody"]}).status_code == 422
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
    set_quota(ctx, "claude-01", five_hour=(95, hours(2)))
    tickets = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=heads["cc"]).json()["tickets"]
    assert [(t["key"], t["step"]) for t in tickets] == [("TESTWP-6", "triage")]


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
    assert provider_routing.evaluate(ctx.db, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(70, hours(2)))  # under the ceiling but not under 60%
    assert provider_routing.evaluate(ctx.db, ["claude", "codex"])["providers"]["claude"]["held_back"]
    set_quota(ctx, "claude-01", five_hour=(55, hours(2)))
    back = provider_routing.evaluate(ctx.db, ["claude", "codex"])["providers"]["claude"]
    assert not back["held_back"]
    assert ctx.db.one("SELECT count(*) AS n FROM provider_routing_state")["n"] == 0


def test_held_back_provider_returns_when_its_window_resets(client, ctx):
    fleet(client, ctx)
    reset = datetime.now(timezone.utc) + timedelta(minutes=30)
    set_quota(ctx, "claude-01", five_hour=(92, reset))
    assert provider_routing.evaluate(ctx.db, ["claude", "codex"])["providers"]["claude"]["held_back"]
    # the window's own reset time passes while the last sample still reports 92%
    later = reset + timedelta(minutes=1)
    after = provider_routing.evaluate(ctx.db, ["claude", "codex"], now=later)["providers"]["claude"]
    assert not after["held_back"] and after["windows"][0]["reset"] is True
    # a fresh sample for the next window counts again
    set_quota(ctx, "claude-01", five_hour=(20, later + timedelta(hours=5)))
    assert not provider_routing.evaluate(ctx.db, ["claude", "codex"], now=later)["providers"]["claude"]["held_back"]


def test_each_window_uses_its_own_reset_time(client, ctx):
    fleet(client, ctx)
    now = datetime.now(timezone.utc)
    set_quota(ctx, "claude-01", five_hour=(95, now + timedelta(hours=1)), seven_day=(30, now + timedelta(days=3)))
    assert provider_routing.evaluate(ctx.db, ["claude", "codex"])["providers"]["claude"]["held_back"]
    # the five hour window resets; the seven day window (30%) does not hold the provider back
    after = provider_routing.evaluate(ctx.db, ["claude", "codex"], now=now + timedelta(hours=2))["providers"]["claude"]
    assert not after["held_back"]
    assert {w["name"]: w["reset"] for w in after["windows"]} == {"five_hour": True, "seven_day": False}


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


def test_held_back_provider_does_not_break_role_coverage(client, ctx):
    fleet(client, ctx)
    set_quota(ctx, "claude-01", five_hour=(97, hours(2)))
    watchdog(ctx)
    assert state(ctx)["claude"]["held_back"]
    # the existing coverage check still counts the held-back account as ready: no role has fewer than two
    for role in ("dev", "verify", "master"):
        assert not ctx.db.one("SELECT 1 AS x FROM incidents WHERE kind='role_coverage' AND subject=%s"
                              " AND closed_at IS NULL", (role,))
    assert ctx.db.one("SELECT status FROM accounts WHERE id='claude-01'")["status"] in ("idle", "working", "probing")


def test_machines_screen_has_the_routing_panel():
    from pathlib import Path
    html = (Path(__file__).parents[2] / "aiflow/v4/control/ui/console.html").read_text()
    for needle in ("AI provider routing", "routing-save", "data-routing-ceiling", "Why each ticket went to its account",
                   "Quota unknown", "/v1/provider-routing/settings"):
        assert needle in html
