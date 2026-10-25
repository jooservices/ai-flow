"""Every event of AI Flow goes to OpenObserve (JSON, one stream), next to the journal line it always had.

    events.configure(settings, worker="JAVM-2")   once per process (worker, control, health)
    events.emit("ticket.transition", ticket="JAVM-1", to="In Review")   anywhere; never raises, never waits for the network

Events are enqueued in memory and handled by a background thread. It writes each batch to a durable JSONL spool before
delivery and removes rows only after OpenObserve accepts them. A failed delivery stays on disk for retry or replay by the
next process. `emit()` never waits for the network. With no URL and login configured nothing is sent. Credentials come
from the environment (`AIFLOW_OBS_USER`, `AIFLOW_OBS_PASSWORD` in Workspaces/.env), never from a repository file."""
from __future__ import annotations

import atexit
import base64
import json
import os
import re
import tempfile
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
                 sender: Callable[[str, bytes, str], None] = post, flush_seconds: float = 2.0, batch: int = 100,
                 thread: bool = True, spool: Path | None = None) -> None:
        self.endpoint = f"{url.rstrip('/')}/api/{org}/{stream}/_json" if url and user and password else ""
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode() if self.endpoint else ""
        self.base, self.sender, self.batch, self.flush_seconds = dict(base or {}), sender, batch, flush_seconds
        self.queue: deque[dict[str, Any]] = deque()
        self.lock = threading.Lock()
        self.spool_lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.failures = 0
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.closed = False
        self.spool = spool if self.endpoint else None
        self.thread = threading.Thread(target=self._loop, name="aiflow-events", daemon=True) if (self.endpoint and thread) else None
        if self.thread:
            if self.spool and self.spool.exists():
                self.wakeup.set()
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
            if event == "ai.run" and "run_id" not in row and row.get("log_file"):
                row["run_id"] = Path(str(row["log_file"])).stem
            if "ticket" not in row:
                found = TICKET.search(str(row.get("message", "")))
                if found:
                    row["ticket"] = found.group(1)
            if "ticket" in row and "project" not in row:
                row["project"] = str(row["ticket"]).rsplit("-", 1)[0]
            with self.lock:
                self.queue.append(row)
            self.wakeup.set()
        except Exception:  # an event never breaks the work
            pass

    def flush(self) -> bool:
        """Persist queued rows before sending; a failed send leaves the JSONL spool ready for retry."""
        with self.flush_lock:
            if not self._stage_queue():
                self.failures += 1
                return False
            while True:
                if self.spool:
                    try:
                        rows = self._spool_rows()[:self.batch]
                    except OSError:
                        self.failures += 1
                        return False
                else:
                    with self.lock:
                        rows = [self.queue.popleft() for _ in range(min(self.batch, len(self.queue)))]
                if not rows:
                    return True
                try:
                    self.sender(self.endpoint, json.dumps(rows).encode(), self.auth)
                except Exception:
                    self.failures += 1
                    if not self.spool:
                        with self.lock:
                            self.queue.extendleft(reversed(rows))
                    return False
                if self.spool:
                    try:
                        self._ack_spool(len(rows))
                    except OSError:
                        # OpenObserve accepted this batch. Retaining it is safer than losing rows if the
                        # acknowledgement could not be made durable; delivery is therefore at least once.
                        self.failures += 1
                        return False
                self.failures = 0

    def _stage_queue(self) -> bool:
        if not self.spool:
            return True
        with self.lock:
            rows = list(self.queue)
            self.queue.clear()
        if not rows:
            return True
        try:
            with self.spool_lock:
                self.spool.parent.mkdir(parents=True, exist_ok=True)
                with self.spool.open("a", encoding="utf-8") as handle:
                    handle.writelines(json.dumps(row) + "\n" for row in rows)
                    handle.flush()
                    os.fsync(handle.fileno())
            return True
        except OSError:
            with self.lock:
                self.queue.extendleft(reversed(rows))
            return False

    def _spool_rows(self) -> list[dict[str, Any]]:
        if not self.spool or not self.spool.is_file():
            return []
        with self.spool_lock:
            lines = self.spool.read_text(encoding="utf-8").splitlines()
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _ack_spool(self, count: int) -> None:
        if not self.spool:
            return
        with self.spool_lock:
            if not self.spool.is_file():
                return
            rows = []
            for line in self.spool.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            remaining = rows[count:]
            if not remaining:
                self.spool.unlink(missing_ok=True)
                return
            self.spool.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.spool.parent, delete=False) as handle:
                temp = Path(handle.name)
                handle.writelines(json.dumps(row) + "\n" for row in remaining)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.spool)

    def _loop(self) -> None:
        delay = self.flush_seconds
        while not self.stop.is_set():
            self.wakeup.wait(min(60.0, delay))
            self.wakeup.clear()
            if self.stop.is_set():
                break
            if self.flush():
                delay = self.flush_seconds
            else:
                delay = min(60.0, self.flush_seconds * (2 ** min(self.failures, 5)))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.wakeup.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.2)
        if self.endpoint:
            if self.spool:
                self._stage_queue()  # shutdown persists locally and never waits for OpenObserve
            else:
                self.flush()


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
