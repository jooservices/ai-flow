"""Delivery of events to OpenObserve: a bounded queue, a background thread, retry with a capped backoff and an incident.

Split from `Events` (JAIF-476): `Events` writes the registry row and hands the event over; this class only ships."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("aiflow4.events")
O2_BATCH_SIZE = 100
O2_RETRY_BASE_SECONDS = 1
O2_RETRY_MAX_SECONDS = 60
O2_WARNING_INTERVAL_SECONDS = 300
O2_INCIDENT_AFTER_SECONDS = 120
O2_INCIDENT_KIND = "openobserve_delivery"


class OpenObserveShipper:
    def __init__(self, url: str = "", basic: str = "", stream: str = "aiflow4_events", source: str = "control") -> None:
        self.url = url.rstrip("/")
        self.basic = basic
        self.stream = stream
        self.source = source or "control"
        self.q: queue.Queue[dict] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._incident_context: Any | None = None
        self._failure_since: float | None = None
        self._last_warning_at: float | None = None
        self._incident_open = False
        self._ever_succeeded = False

    def bind_incident_context(self, ctx: Any) -> None:
        """Allow the shipping thread to open/resolve registry incidents after sustained delivery failures."""
        self._incident_context = ctx

    def enqueue(self, out: dict) -> None:
        if self.url:
            try:
                self.q.put_nowait(out)
            except queue.Full:
                log.warning("event queue full; OpenObserve copy of %s dropped", out["type"])

    def start(self) -> None:
        if self.url and not self._thread:
            self._thread = threading.Thread(target=self._ship, name="events-o2", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ship(self) -> None:
        client = httpx.Client(timeout=10, headers={"Authorization": f"Basic {self.basic}"})
        batch: list[dict] = []
        retries = 0
        try:
            while not self._stop.is_set():
                if not batch:
                    try:
                        batch.append(self.q.get(timeout=2))
                    except queue.Empty:
                        continue
                    while len(batch) < O2_BATCH_SIZE:
                        try:
                            batch.append(self.q.get_nowait())
                        except queue.Empty:
                            break
                try:
                    r = client.post(f"{self.url}/api/default/{self.stream}/_json",
                                    content=json.dumps(batch, default=str))
                    if r.status_code < 300:
                        batch = []
                        retries = 0
                        self._delivery_succeeded()
                        continue
                    detail = f"{r.status_code} {r.text[:200]}"
                except httpx.HTTPError as e:
                    detail = f"unreachable: {e}"

                retries += 1
                self._delivery_failed(detail)
                self._wait_for_retry(self.retry_delay(retries))
        finally:
            client.close()

    @staticmethod
    def retry_delay(retries: int) -> int:
        """Exponential retry delay with a fixed upper bound; the failed batch remains intact while retrying."""
        power = min(max(0, retries - 1), O2_RETRY_MAX_SECONDS.bit_length())
        return min(O2_RETRY_BASE_SECONDS * 2 ** power, O2_RETRY_MAX_SECONDS)

    def _wait_for_retry(self, seconds: int) -> bool:
        return self._stop.wait(seconds)

    def _delivery_failed(self, detail: str, now_at: float | None = None) -> None:
        now_at = time.monotonic() if now_at is None else now_at
        if self._failure_since is None:
            self._failure_since = now_at
        duration = max(0, now_at - self._failure_since)
        if self._last_warning_at is None or now_at - self._last_warning_at >= O2_WARNING_INTERVAL_SECONDS:
            log.warning("OpenObserve event delivery failing for %s/%s (%.0fs); retrying: %s",
                        self.source, self.stream, duration, detail)
            self._last_warning_at = now_at
        if duration >= O2_INCIDENT_AFTER_SECONDS:
            self._open_delivery_incident(detail, duration)

    def _delivery_succeeded(self, now_at: float | None = None) -> None:
        if self._failure_since is None and self._ever_succeeded:
            return
        if self._failure_since is not None:
            duration = max(0, (time.monotonic() if now_at is None else now_at) - self._failure_since)
            log.info("OpenObserve event delivery recovered for %s/%s after %.0fs",
                     self.source, self.stream, duration)
        if self._incident_context is not None:
            try:
                from .incidents import close_incident
                close_incident(self._incident_context, O2_INCIDENT_KIND, self._incident_subject(),
                               "OpenObserve accepted an event batch again")
            except Exception:  # noqa: BLE001 - observability recovery must not stop event shipping
                log.exception("could not resolve OpenObserve delivery incident for %s", self.source)
        self._incident_open = False
        self._failure_since = None
        self._last_warning_at = None
        self._ever_succeeded = True

    def _open_delivery_incident(self, detail: str, duration: float) -> None:
        if self._incident_open or self._incident_context is None:
            return
        try:
            from .incidents import open_incident
            title = f"OpenObserve is rejecting events from {self.source}"
            incident_detail = (f"Stream {self.stream} has rejected event batches continuously for at least "
                               f"{int(duration)} seconds. The last response was: {detail[:300]}")
            open_incident(self._incident_context, O2_INCIDENT_KIND, self._incident_subject(), "warning",
                          title, incident_detail)
            self._incident_open = True
        except Exception:  # noqa: BLE001 - observability alerting must not stop event shipping
            log.exception("could not open OpenObserve delivery incident for %s", self.source)

    def _incident_subject(self) -> str:
        return f"{self.source}:{self.stream}"
