"""The production OpenObserve dashboard SQL is executable and time-bounded for every panel."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from aiflow import events as legacy_events
from aiflow.memory.workflow import MemoryWorkflow

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "OBSERVABILITY.md"
DASHBOARD = ROOT / "deploy" / "v4" / "o2" / "dashboards" / "production-health-slo.json"
PANEL_SQL = re.compile(r"<!-- dashboard-panel: (.*?) -->\s*```sql\s*\n(.*?)\n```", re.DOTALL)
EXPECTED_PANELS = {
    "Blocked by signature",
    "Time in each status",
    "Done without human action",
    "Rework per ticket",
    "Master rescues and result",
    "Repeated signatures after a study case",
    "AI minutes",
    "Tokens",
}


def _dashboard_panels() -> dict[str, dict]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return {panel["title"]: panel for tab in dashboard["tabs"] for panel in tab["panels"]}


def _documented_queries() -> dict[str, str]:
    return {title: " ".join(sql.split()) for title, sql in PANEL_SQL.findall(DOCS.read_text(encoding="utf-8"))}


def test_production_dashboard_covers_all_metrics_and_uses_documented_seven_day_sql():
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panels = _dashboard_panels()
    queries = _documented_queries()

    assert dashboard["defaultDatetimeDuration"] == {"type": "relative", "relativeTimePeriod": "7d"}
    assert set(panels) == EXPECTED_PANELS
    assert set(queries) == EXPECTED_PANELS
    for title, panel in panels.items():
        query = panel["queries"][0]
        normalized = " ".join(query["query"].split())
        assert panel["queryType"] == "sql"
        assert query["fields"]["stream"] == "aiflow_events"
        assert f'FROM "aiflow_events"' in query["query"]
        assert "_timestamp >= now() - INTERVAL '7 days'" in query["query"]
        assert normalized == queries[title]
        assert query["fields"]["x"] and query["fields"]["y"]


def _insert_event(db, event: str, ticket: str, age: str = "1 day", **fields) -> None:
    allowed = {
        "kind", "reason", "from_status", "minutes_in_from", "source", "round", "headline", "incident",
        "result", "minutes", "agent", "role", "found", "case_ids", "usage_known", "input", "output", "total",
    }
    assert set(fields) <= allowed
    names = ["_timestamp", "event", "ticket", *fields]
    quoted_names = ", ".join(f'"{name}"' for name in names)
    placeholders = ", ".join(["now() - (%s::interval)", *(["%s"] * (2 + len(fields)))])
    db.run(f'INSERT INTO "aiflow_events" ({quoted_names}) VALUES ({placeholders})',
           (age, event, ticket, *fields.values()))


def test_every_dashboard_panel_returns_seven_day_data(db):
    db.run(
        'CREATE TABLE "aiflow_events" ('
        '"_timestamp" timestamptz NOT NULL, "event" text, "ticket" text, "kind" text, "reason" text, '
        '"from_status" text, "minutes_in_from" numeric, "source" text, "round" integer, "headline" text, '
        '"incident" text, "result" text, "minutes" numeric, "agent" text, "role" text, "found" integer, '
        '"case_ids" text, "usage_known" boolean, "input" bigint, "output" bigint, "total" bigint)'
    )

    _insert_event(db, "ticket.blocked", "JAIF-1", kind="Attempts", reason="three failed verify attempts")
    _insert_event(db, "ticket.blocked", "JAIF-2", kind="Attempts", reason="three failed verify attempts")
    _insert_event(db, "ticket.blocked", "JAIF-OLD", age="8 days", kind="Attempts", reason="three failed verify attempts")

    _insert_event(db, "ticket.status", "JAIF-3", from_status="In Progress", minutes_in_from=10, source="supervisor")
    _insert_event(db, "ticket.status", "JAIF-4", from_status="To Do", minutes_in_from=15, source="observed")
    _insert_event(db, "ticket.status", "JAIF-OLD", age="8 days", from_status="To Do", minutes_in_from=999,
                  source="supervisor")

    _insert_event(db, "ticket.done", "JAIF-5")
    _insert_event(db, "ticket.done", "JAIF-6")
    _insert_event(db, "ticket.human_review", "JAIF-6")
    _insert_event(db, "ticket.done", "JAIF-OLD", age="8 days")

    _insert_event(db, "ticket.rework", "JAIF-7", round=1, headline="missing regression coverage")
    _insert_event(db, "ticket.rework", "JAIF-7", round=2, headline="missing regression coverage")
    _insert_event(db, "ticket.rework", "JAIF-OLD", age="8 days", round=9, headline="stale rework")

    _insert_event(db, "operator.run", "", incident="blocked:JAIF-8", result="RESOLVED", minutes=3)
    _insert_event(db, "operator.run", "", age="8 days", incident="blocked:JAIF-OLD", result="ESCALATE", minutes=30)

    for key in ("JAIF-9", "JAIF-10"):
        _insert_event(db, "memory.recall", key, found=2, case_ids="a1b2c3d4")
        _insert_event(db, "ticket.rework", key, round=1, headline="repeated nonce validation failure")
    _insert_event(db, "memory.recall", "JAIF-11", found=2, case_ids="")
    _insert_event(db, "ticket.rework", "JAIF-11", round=1, headline="repeated nonce validation failure")
    _insert_event(db, "memory.recall", "JAIF-OLD", age="8 days", found=1, case_ids="a1b2c3d4")
    _insert_event(db, "ticket.rework", "JAIF-OLD", age="8 days", round=1,
                  headline="repeated nonce validation failure")

    _insert_event(db, "ai.run", "JAIF-12", agent="Codex", role="developer", minutes=2.5)
    _insert_event(db, "ai.run", "JAIF-OLD", age="8 days", agent="Codex", role="developer", minutes=99)
    _insert_event(db, "ai.usage", "JAIF-12", agent="Codex", role="developer", usage_known=True,
                  input=4000, output=2000, total=6000)
    _insert_event(db, "ai.usage", "JAIF-13", agent="Grok", role="developer", usage_known=False,
                  input=0, output=0, total=0)
    _insert_event(db, "ai.usage", "JAIF-OLD", age="8 days", agent="Codex", role="developer", usage_known=True,
                  input=90000, output=90000, total=180000)

    results = {
        title: db.all(sql)
        for title, sql in _documented_queries().items()
    }
    assert set(results) == EXPECTED_PANELS
    assert all(rows for rows in results.values()), "every panel query must return a row from the seven-day fixture"
    assert results["Blocked by signature"][0]["blocked"] == 2
    assert results["Time in each status"][0]["total_minutes"] == 15
    done = results["Done without human action"][0]
    assert (done["done_tickets"], done["done_without_human_action"], done["done_without_human_action_pct"]) == (2, 1, 50.0)
    assert results["Rework per ticket"][0]["rework_events"] == 2
    assert results["Master rescues and result"][0]["rescue_runs"] == 1
    repeated = results["Repeated signatures after a study case"][0]
    assert (repeated["repeats"], repeated["affected_tickets"]) == (2, 2)
    assert results["AI minutes"][0]["ai_minutes"] == 2.5
    assert results["Tokens"][0]["total_tokens"] == 6000


def test_memory_recall_event_identifies_recalled_study_cases(monkeypatch):
    emitted = []
    monkeypatch.setattr(legacy_events, "emit", lambda event, **fields: emitted.append((event, fields)))

    class Memory:
        enabled = True

        def search(self, *_args, **_kwargs):
            return [
                {"id": "case-12345678", "kind": "case", "title": "Prior case", "project": "JAIF",
                 "namespace": "JOO", "status": "verified", "body": "A study case."},
                {"id": "lesson-abcdef12", "kind": "lesson", "title": "Lesson", "project": "JAIF",
                 "namespace": "JOO", "status": "verified", "body": "A lesson."},
            ]

    class Redis:
        def set(self, *_args):
            return None

    workflow = object.__new__(MemoryWorkflow)
    workflow.default = Memory()
    workflow.default_url = ""
    workflow.clients = {}
    workflow.cfg = SimpleNamespace(memory={}, project=lambda _key: SimpleNamespace(company="JOO", jira_key="JAIF"))
    workflow.redis = Redis()

    workflow.recall("JAIF-14", {"summary": "Example", "description": ""}, SimpleNamespace(name="ai-flow"))

    assert emitted == [("memory.recall", {"ticket": "JAIF-14", "found": 2,
                                          "ids": "case-123,lesson-a", "case_ids": "case-123"})]
