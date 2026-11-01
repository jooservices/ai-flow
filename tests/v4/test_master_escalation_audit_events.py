"""An accepted master escalation publishes two events whose audit rows match the old rows."""
from __future__ import annotations

from conftest import join_node

MASTER_SCOPE = "master:global"


def join(client, name):
    response = join_node(client, name)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['node_token']}"}


def ready(client, node, account):
    headers = join(client, node)
    response = client.post(f"/v1/nodes/{node}/heartbeat", json={
        "workers": [{"account": account, "state": "idle"}], "slots_total": 2,
        "metrics": {"hostname": node, "load1": 0.1, "cpus": 4, "mem_total": 100, "mem_used": 20,
                    "disk_total": 100 * 1024 ** 3, "disk_free": 20 * 1024 ** 3, "disk_free_pct": 20},
    }, headers=headers)
    assert response.status_code == 200
    return headers


def events_of(db, event_type):
    return db.all("SELECT type,aggregate,aggregate_id,payload FROM domain_events WHERE type=%s ORDER BY seq",
                  (event_type,))


def test_an_escalation_publishes_the_request_and_the_notification_with_the_same_audit_rows(client, ctx, monkeypatch):
    cc = ready(client, "cc", "claude-01")
    master = client.post("/v1/work/claim", json={"ticket": MASTER_SCOPE, "account": "claude-01", "step": "master"},
                         headers=cc).json()
    monkeypatch.setattr("aiflow.v4.control.notify.configured_channels", lambda *_args, **_kwargs: ["email"])
    monkeypatch.setattr("aiflow.v4.control.notify.send", lambda *args, **_kwargs: ["email"])
    options = ["Add a Codex account", "Keep the role uncovered"]
    intent = "choose whether to add another provider account"
    why = "The master cannot provision or sign in an account"
    response = client.post("/v1/master/actions", json={
        "run_id": master["run_id"], "fence": master["fence"], "action": "escalate_to_owner", "intent": intent,
        "why": why, "options": options, "observation_key": "incident:role_coverage:audit-events"}, headers=cc)
    assert response.status_code == 200, response.text

    requested = events_of(ctx.db, "master.escalation_requested")
    assert len(requested) == 1 and requested[0]["aggregate"] == "run"
    assert requested[0]["aggregate_id"] == master["run_id"]
    assert requested[0]["payload"]["action"] == "escalate_to_owner" and requested[0]["payload"]["options"] == options
    notified = events_of(ctx.db, "master.escalation_notified")
    assert len(notified) == 1 and notified[0]["payload"]["result"] == "email"
    assert notified[0]["payload"]["observation_key"] == "incident:role_coverage:audit-events"

    master_row = ctx.db.one("SELECT target,action,ticket,run_id,fence,actor,accepted,result,payload FROM write_audit "
                            "WHERE run_id=%s AND action='escalate_to_owner'", (master["run_id"],))
    assert master_row["target"] == "master" and master_row["accepted"] is True and master_row["fence"] == master["fence"]
    assert master_row["payload"] == {"intent_chars": len(intent), "options": options,
                                     "observation_key": "incident:role_coverage:audit-events"}
    notify_row = ctx.db.one("SELECT target,action,run_id,fence,accepted,result,payload FROM write_audit "
                            "WHERE run_id=%s AND action='master-escalation'", (master["run_id"],))
    assert notify_row["target"] == "notify" and notify_row["accepted"] is True and notify_row["result"] == "email"
    assert notify_row["payload"]["pending_channels"] == [] and notify_row["payload"]["options"] == options
    assert notify_row["payload"]["observation_key"] == "incident:role_coverage:audit-events"
    # the two rows keep the old order: the master decision first, then the notification
    order = [row["action"] for row in ctx.db.all(
        "SELECT action FROM write_audit WHERE run_id=%s AND action IN ('escalate_to_owner','master-escalation') ORDER BY id",
        (master["run_id"],))]
    assert order == ["escalate_to_owner", "master-escalation"]
