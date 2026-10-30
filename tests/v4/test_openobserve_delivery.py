import json
from pathlib import Path

from aiflow.v4.control import openobserve_shipper as shipper_module
from aiflow.v4.control.events import Events
from aiflow.v4.core.settings import Settings


ROOT = Path(__file__).resolve().parents[2]


def test_lab_openobserve_memory_budget_and_exporter_sources_are_explicit():
    env = dict(line.split("=", 1) for line in (ROOT / "deploy/v4/o2/lab.env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert env["ZO_MEM_TABLE_MAX_SIZE"] == "512"
    assert env["ZO_MAX_FILE_SIZE_IN_MEMORY"] == "128"
    assert env["ZO_MEMORY_CACHE_MAX_SIZE"] == "64"
    assert env["ZO_MEMORY_CACHE_DATAFUSION_MAX_SIZE"] == "128"

    compose = (ROOT / "deploy/v4/control/compose.yaml").read_text()
    for source in ("control-api-a", "control-api-b", "control-loops"):
        assert f"AIFLOW4_O2_SOURCE: {source}" in compose


def test_o2_source_is_read_from_settings(monkeypatch):
    monkeypatch.setenv("AIFLOW4_SECRET_FILES", "")
    monkeypatch.setenv("AIFLOW4_O2_SOURCE", "control-api-b")
    assert Settings.from_env().o2_source == "control-api-b"


def test_failed_batch_is_preserved_and_retry_delay_has_a_hard_cap(db, monkeypatch, caplog):
    class Response:
        status_code = 503
        text = "MemoryTableOverflowError"

    class Client:
        def __init__(self, **kwargs):
            self.payloads = []
            self.closed = False

        def post(self, url, content):
            self.payloads.append((url, content))
            return Response()

        def close(self):
            self.closed = True

    fake = Client()
    monkeypatch.setattr(shipper_module.httpx, "Client", lambda **kwargs: fake)
    events = Events(db, "http://o2", source="test-exporter")
    for idx in range(101):
        events.shipper.q.put_nowait({"type": "test.event", "number": idx})
    waits = []

    def stop_after_retries(seconds):
        waits.append(seconds)
        if len(waits) == 8:
            events.shipper._stop.set()
        return events.shipper._stop.is_set()

    monkeypatch.setattr(events.shipper, "_wait_for_retry", stop_after_retries)
    caplog.set_level("WARNING", logger="aiflow4.events")
    events.shipper._ship()

    payloads = [json.loads(content) for _, content in fake.payloads]
    assert all(payload == payloads[0] for payload in payloads)
    assert len(payloads[0]) == 100
    assert events.shipper.q.qsize() == 1  # newly queued events are not appended to a batch being retried
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
    assert len([record for record in caplog.records if "OpenObserve event delivery failing" in record.message]) == 1
    assert fake.closed


def test_continuous_rejection_opens_one_incident_and_success_resolves_it(ctx, monkeypatch, caplog):
    from aiflow.v4.control import incidents

    notifications = []
    monkeypatch.setattr(incidents.notify, "send", lambda *args, **kwargs: notifications.append(args) or ["test"])
    events = Events(ctx.db, source="control-api-a")
    events.bind_incident_context(ctx)
    caplog.set_level("WARNING", logger="aiflow4.events")

    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=100.0)
    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=130.0)
    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=219.9)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='openobserve_delivery' AND closed_at IS NULL")["n"] == 0

    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=220.0)
    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=225.0)
    incident = ctx.db.one("SELECT * FROM incidents WHERE kind='openobserve_delivery' AND closed_at IS NULL")
    assert incident["subject"] == "control-api-a:aiflow4_events"
    assert incident["severity"] == "warning"
    assert "120 seconds" in incident["detail"]
    assert len(notifications) == 1
    assert len([record for record in caplog.records if "OpenObserve event delivery failing" in record.message]) == 1

    events.shipper._delivery_failed("503 MemoryTableOverflowError", now_at=400.0)
    assert len([record for record in caplog.records if "OpenObserve event delivery failing" in record.message]) == 2
    events.shipper._delivery_succeeded(now_at=421.0)
    assert ctx.db.one("SELECT closed_at FROM incidents WHERE id=%s", (incident["id"],))["closed_at"] is not None


def test_first_successful_batch_closes_incident_left_open_by_a_restarted_exporter(ctx, monkeypatch):
    from aiflow.v4.control import incidents

    monkeypatch.setattr(incidents.notify, "send", lambda *args, **kwargs: ["test"])
    incidents.open_incident(ctx, "openobserve_delivery", "control-api-a:aiflow4_events", "warning",
                            "OpenObserve rejecting events", "exporter restarted during the outage")
    events = Events(ctx.db, source="control-api-a")
    events.bind_incident_context(ctx)

    events.shipper._delivery_succeeded(now_at=200.0)

    incident = ctx.db.one("SELECT closed_at FROM incidents WHERE kind='openobserve_delivery'")
    assert incident["closed_at"] is not None
