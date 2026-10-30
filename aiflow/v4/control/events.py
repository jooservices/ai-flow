"""Events: one row per thing that happened, stored in the registry and streamed to OpenObserve.

Every event shares the fields ts, type, ticket, project, step, run_id, fence, account, provider, model, node,
duration_ms, outcome, reason (design: stream aiflow4_events). OpenObserve gets them in batches from a
background thread (`openobserve_shipper.OpenObserveShipper`); when it is down, events stay in the registry and the batch
is retried. Node heartbeats go through ship() instead: OpenObserve only, so they neither flood the Command Center nor grow
the events table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import DB, J
from .openobserve_shipper import OpenObserveShipper

COLUMNS = ("type", "ticket", "project", "step", "run_id", "fence", "account", "provider", "model", "node",
           "duration_ms", "outcome", "reason")


class Events:
    def __init__(self, db: DB, o2_url: str = "", o2_basic: str = "", stream: str = "aiflow4_events",
                 source: str = "control") -> None:
        self.db = db
        self.shipper = OpenObserveShipper(o2_url, o2_basic, stream, source)

    def bind_incident_context(self, ctx: Any) -> None:
        self.shipper.bind_incident_context(ctx)

    def start(self) -> None:
        self.shipper.start()

    def stop(self) -> None:
        self.shipper.stop()

    def emit(self, type: str, **fields: Any) -> dict:
        row = {k: fields.pop(k, None) for k in COLUMNS if k != "type"}
        row["type"] = type
        data = fields.pop("data", {}) or {}
        data.update(fields)
        rec = self.db.one(
            "INSERT INTO events (type, ticket, project, step, run_id, fence, account, provider, model, node, duration_ms,"
            " outcome, reason, data) VALUES (%(type)s, coalesce(%(ticket)s,''), coalesce(%(project)s,''), coalesce(%(step)s,''),"
            " coalesce(%(run_id)s,''), %(fence)s, coalesce(%(account)s,''), coalesce(%(provider)s,''), coalesce(%(model)s,''),"
            " coalesce(%(node)s,''), %(duration_ms)s, coalesce(%(outcome)s,''), coalesce(%(reason)s,''), %(data)s) RETURNING id, ts",
            {**row, "data": J(data)})
        out = {**{k: v for k, v in row.items() if v not in (None, "")}, **data, "id": rec["id"],
               "_timestamp": int(rec["ts"].timestamp() * 1_000_000)}
        self.shipper.enqueue(out)
        return out

    def ship(self, type: str, **fields: Any) -> dict:
        """High-volume telemetry (node heartbeats): OpenObserve only, never a registry row."""
        out = {k: v for k, v in fields.items() if v not in (None, "")}
        out["type"] = type
        out["_timestamp"] = int(now().timestamp() * 1_000_000)
        self.shipper.enqueue(out)
        return out


def now() -> datetime:
    return datetime.now(timezone.utc)
