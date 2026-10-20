"""Every event of AI Flow goes to OpenObserve (JSON, one stream), next to the journal line it always had.

    events.configure(settings, worker="JAVM-2")   once per process (worker, control, health)
    events.emit("ticket.transition", ticket="JAVM-1", to="In Review")   anywhere; never raises, never waits for the network

Events are queued in memory and sent in batches by a background thread. A failing OpenObserve loses nothing for a while (the
queue keeps the newest events and drops the oldest when it is full) and never slows or stops a worker. With no URL configured
nothing is sent. Credentials come from the environment (`AIFLOW_OBS_USER`, `AIFLOW_OBS_PASSWORD` in Workspaces/.env), never from a file of the repository."""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable

MAX_TEXT = 2000
TICKET = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def post(url: str, body: bytes, auth: str, timeout: int = 8) -> None:
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    urllib.request.urlopen(request, timeout=timeout).close()


class Events:
    def __init__(self, url: str, org: str, stream: str, user: str, password: str, base: dict[str, Any] | None = None,
                 sender: Callable[[str, bytes, str], None] = post, flush_seconds: float = 2.0, batch: int = 100, capacity: int = 5000,
                 thread: bool = True, spool: Path | None = None) -> None:
        self.endpoint = f"{url.rstrip('/')}/api/{org}/{stream}/_json" if url and user and password else ""
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode() if self.endpoint else ""
        self.base, self.sender, self.batch, self.flush_seconds = dict(base or {}), sender, batch, flush_seconds
        self.queue: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.dropped = 0
        self.failures = 0
        self.stop = threading.Event()
        self.spool = spool if self.endpoint else None
        self.reload_spool()
        self.thread = threading.Thread(target=self._loop, name="aiflow-events", daemon=True) if (self.endpoint and thread) else None
        if self.thread:
            self.thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def emit(self, event: str, **fields: Any) -> None:
        if not self.endpoint:
            return
        try:
            row: dict[str, Any] = {**self.base, "_timestamp": int(time.time() * 1_000_000), "event": event}
            for name, value in fields.items():
                if value is None:
                    continue
                row[name] = value if isinstance(value, (int, float, bool)) else str(value)[:MAX_TEXT]
            if "ticket" not in row:
                found = TICKET.search(str(row.get("message", "")))
                if found:
                    row["ticket"] = found.group(1)
            if "ticket" in row and "project" not in row:
                row["project"] = str(row["ticket"]).rsplit("-", 1)[0]
            with self.lock:
                if len(self.queue) == self.queue.maxlen:
                    self.dropped += 1
                self.queue.append(row)
        except Exception:  # an event never breaks the work
            pass

    def flush(self) -> bool:
        """Send everything queued, batch by batch. False when OpenObserve did not accept a batch (the rows stay for the next try)."""
        while True:
            with self.lock:
                rows = [self.queue.popleft() for _ in range(min(self.batch, len(self.queue)))]
            if not rows:
                return True
            try:
                self.sender(self.endpoint, json.dumps(rows).encode(), self.auth)
                self.failures = 0
            except Exception:
                self.failures += 1
                with self.lock:  # keep them, oldest first, unless the queue filled up meanwhile
                    room = (self.queue.maxlen or 0) - len(self.queue)
                    self.queue.extendleft(reversed(rows[-room:] if room > 0 else []))
                    self.dropped += max(0, len(rows) - max(room, 0))
                return False

    def _loop(self) -> None:
        while not self.stop.wait(min(60.0, self.flush_seconds * (2 ** min(self.failures, 5)))):
            self.flush()

    def reload_spool(self) -> None:
        """Rows an earlier process could not send (OpenObserve down when it stopped) are sent by this one."""
        if not self.spool or not self.spool.is_file():
            return
        try:
            lines = self.spool.read_text(encoding="utf-8").splitlines()
            self.spool.unlink()
        except OSError:
            return
        for line in lines:
            try:
                self.queue.append(json.loads(line))
            except ValueError:
                continue

    def close(self) -> None:
        self.stop.set()
        if self.endpoint:
            if not self.flush() and self.spool:  # keep what could not be sent for the next process
                with self.lock:
                    rows = list(self.queue)
                    self.queue.clear()
                try:
                    self.spool.parent.mkdir(parents=True, exist_ok=True)
                    with self.spool.open("a", encoding="utf-8") as handle:
                        handle.writelines(json.dumps(row) + "\n" for row in rows)
                except OSError:
                    pass


_sink: Events | None = None


def configure(obs: dict[str, str], machine: str, worker: str, spool_dir: Path | None = None, **base: Any) -> Events:
    """Start the process-wide sink from the AIFLOW_OBS_* settings (disabled when URL, user or password is missing)."""
    global _sink
    if _sink is not None:
        _sink.close()
    _sink = Events(obs.get("AIFLOW_OBS_URL", ""), obs.get("AIFLOW_OBS_ORG", "default"), obs.get("AIFLOW_OBS_STREAM", "aiflow_events"),
                   obs.get("AIFLOW_OBS_USER", ""), obs.get("AIFLOW_OBS_PASSWORD", ""),
                   {"service": "aiflow", "machine": machine, "worker": worker, "pid": os.getpid(), **base},
                   spool=spool_dir / f"events-{machine}-{worker}.jsonl" if spool_dir else None)
    atexit.register(_sink.close)
    return _sink


def emit(event: str, **fields: Any) -> None:
    if _sink is not None:
        _sink.emit(event, **fields)


def close() -> None:
    if _sink is not None:
        _sink.close()
