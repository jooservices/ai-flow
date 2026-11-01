"""docs/v4/events.md is generated from the catalog and the registry (JAIF-464)."""
import os
from pathlib import Path

from aiflow.v4.control.features.tickets import events as _tickets_events  # noqa: F401  (registers TicketMoved)
from aiflow.v4.control.features.backups import events as _backups_events  # noqa: F401  (registers the backups events)
from aiflow.v4.control.features.scale_proof import events as _scale_proof_events  # noqa: F401  (registers the scale proof event)
from aiflow.v4.control.features.business import events as _business_events  # noqa: F401  (registers the business events)
from aiflow.v4.control.features.knowledge import events as _knowledge_events  # noqa: F401  (registers the knowledge events)
from aiflow.v4.control.features.console import events as _console_events  # noqa: F401  (registers console audit events)
from aiflow.v4.control.features.fleet import events as _fleet_events  # noqa: F401  (registers fleet audit events)
from aiflow.v4.control.features.runs import events as _runs_events  # noqa: F401  (registers master audit events)
from aiflow.v4.control.features.github_ci_log import events as _github_ci_log_events  # noqa: F401  (registers CI log audit event)
from aiflow.v4.control.features.projects import events as _projects_events  # noqa: F401  (registers project audit events)
from aiflow.v4.control.platform.events import CATALOG, docgen

DOC = Path(__file__).resolve().parents[2] / "docs/v4/events.md"


def test_the_events_document_matches_the_catalog_and_the_listeners():
    text = docgen.render()
    if os.environ.get("AIFLOW_WRITE_EVENTS_DOC") == "1":
        DOC.write_text(text)
    assert DOC.read_text() == text, "docs/v4/events.md is stale: AIFLOW_WRITE_EVENTS_DOC=1 uv run pytest tests/v4/test_events_doc.py"


def test_every_event_in_the_catalog_has_an_owner_aggregate_and_a_documented_row():
    text = docgen.render()
    for event_type, cls in CATALOG.items():
        if event_type.startswith("demo."):
            continue
        assert cls.owner and cls.aggregate
        assert f"`{event_type}`" in text
