"""Events: one row per thing that happened, stored in the registry and streamed to OpenObserve.

Every event shares the fields ts, type, ticket, project, step, run_id, fence, account, provider, model, node,
duration_ms, outcome, reason (design: stream aiflow4_events). OpenObserve gets them in batches from a
background thread; when it is down, events stay in the registry and the batch is retried.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import DB, J

log = logging.getLogger("aiflow4.events")
COLUMNS = ("type", "ticket", "project", "step", "run_id", "fence", "account", "provider", "model", "node",
           "duration_ms", "outcome", "reason")


class Events:
    def __init__(self, db: DB, o2_url: str = "", o2_basic: str = "", stream: str = "aiflow4_events") -> None:
        self.db = db
        self.o2_url = o2_url.rstrip("/")
        self.o2_basic = o2_basic
        self.stream = stream
        self.q: queue.Queue[dict] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        if self.o2_url:
            try:
                self.q.put_nowait(out)
            except queue.Full:
                log.warning("event queue full; OpenObserve copy dropped (registry keeps it)")
        return out

    # --- OpenObserve shipping -------------------------------------------------------------------------------
    def start(self) -> None:
        if self.o2_url and not self._thread:
            self._thread = threading.Thread(target=self._ship, name="events-o2", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ship(self) -> None:
        client = httpx.Client(timeout=10, headers={"Authorization": f"Basic {self.o2_basic}"})
        batch: list[dict] = []
        while not self._stop.is_set():
            try:
                batch.append(self.q.get(timeout=2))
                while len(batch) < 500:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            if not batch:
                continue
            try:
                r = client.post(f"{self.o2_url}/api/default/{self.stream}/_json",
                                content=json.dumps(batch, default=str))
                if r.status_code < 300:
                    batch = []
                else:
                    log.warning("OpenObserve rejected events: %s %s", r.status_code, r.text[:200])
                    time.sleep(5)
            except httpx.HTTPError as e:
                log.warning("OpenObserve unreachable: %s", e)
                time.sleep(5)
            if len(batch) > 5000:
                batch = batch[-5000:]


def now() -> datetime:
    return datetime.now(timezone.utc)
