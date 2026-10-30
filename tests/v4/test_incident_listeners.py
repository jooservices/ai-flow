"""Opening an incident publishes IncidentOpened; telemetry, study case and alert are its listeners (JAIF-477)."""
from aiflow.v4.control import incidents, notify
from aiflow.v4.control.platform.events import wiring


def test_a_new_incident_publishes_one_event_and_its_listeners_run(ctx, db, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda severity, title, detail="", link="", **kw: sent.append(
        (severity, title, kw.get("alert_kind"))) or ["email"])

    assert incidents.open_incident(ctx, "node_down", "cc", "critical", "node cc is down", "No heartbeat.") is True

    event = db.one("SELECT payload FROM domain_events WHERE type='incident.opened'")
    assert event["payload"]["kind"] == "node_down" and event["payload"]["subject"] == "cc"
    telemetry = db.one("SELECT outcome, data FROM events WHERE type='incident.opened'")
    assert telemetry["outcome"] == "critical" and telemetry["data"]["subject"] == "cc"
    assert sent == [("critical", "node cc is down", "critical_incident")]
    assert db.one("SELECT notified FROM incidents WHERE kind='node_down'")["notified"] is True
    assert db.one("SELECT count(*) AS n FROM knowledge_items WHERE source_type='incident'")["n"] == 1


def test_an_incident_that_is_already_open_publishes_nothing_more(ctx, db, monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: ["email"])
    incidents.open_incident(ctx, "node_down", "cc", "critical", "node cc is down")

    assert incidents.open_incident(ctx, "node_down", "cc", "critical", "node cc is down") is False

    assert db.one("SELECT count(*) AS n FROM domain_events WHERE type='incident.opened'")["n"] == 1


def test_an_info_incident_is_recorded_but_not_alerted(ctx, db, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or ["email"])

    incidents.open_incident(ctx, "release_note", "v4.9", "info", "under observed")

    assert sent == []
    assert db.one("SELECT notified FROM incidents WHERE kind='release_note'")["notified"] is False


def test_a_failing_alert_is_retried_by_the_dispatcher_and_does_not_fail_the_caller(ctx, db, monkeypatch):
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mail relay down")
        return ["email"]

    monkeypatch.setattr(notify, "send", flaky)

    assert incidents.open_incident(ctx, "node_down", "cc", "critical", "node cc is down") is True

    assert db.one("SELECT attempts FROM domain_event_deliveries WHERE listener='incidents.alert'")["attempts"] == 1
    db.run("UPDATE domain_event_deliveries SET next_attempt_at=now() - interval '1 second'")
    wiring.dispatcher_for(ctx).run_once()
    assert calls["n"] == 2 and db.one("SELECT notified FROM incidents WHERE kind='node_down'")["notified"] is True
