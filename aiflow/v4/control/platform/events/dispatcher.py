"""Deliver outbox events to their listeners: ordered per aggregate, at least once, with retry and a dead letter.

`Dispatcher.run_once()` takes due deliveries one at a time. A delivery runs in its own transaction with the listener's
database writes inside a savepoint: success commits the listener's writes together with the `done` mark; a failure rolls
the writes back and schedules a retry with exponential backoff; after `max_attempts` the delivery is `dead`, the
`on_dead` callback is called (the incident), and later events of the aggregate are no longer held back by it.
Ordering: for one listener, an event is delivered only after every earlier pending delivery of the same aggregate."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .catalog import Envelope, from_payload
from .registry import DEFAULT_REGISTRY, Registry

log = logging.getLogger("aiflow4.events")
MAX_BACKOFF_S = 3600

# The next due delivery whose listener has no earlier pending delivery for the same aggregate.
_DUE_SQL = """
SELECT d.event_id, d.listener, d.attempts, e.seq, e.id, e.type, e.version, e.occurred_at, e.aggregate, e.aggregate_id,
       e.correlation_id, e.causation_id, e.payload
FROM domain_event_deliveries d JOIN domain_events e ON e.id = d.event_id
WHERE d.status = 'pending' AND d.next_attempt_at <= %s AND (%s::text IS NULL OR d.event_id = %s)
  AND NOT EXISTS (SELECT 1 FROM domain_event_deliveries p JOIN domain_events pe ON pe.id = p.event_id
                  WHERE p.listener = d.listener AND p.status = 'pending' AND pe.aggregate = e.aggregate
                    AND pe.aggregate_id = e.aggregate_id AND pe.seq < e.seq)
ORDER BY e.seq
LIMIT 1
FOR UPDATE OF d SKIP LOCKED
"""


class Dispatcher:
    def __init__(self, db: Any, registry: Registry = DEFAULT_REGISTRY, *, max_attempts: int = 6,
                 base_delay_s: float = 5.0, on_dead: Callable[[Envelope, str, str], None] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.registry = registry
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.on_dead = on_dead
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(self, limit: int = 100, event_id: str | None = None) -> int:
        """Attempt up to `limit` due deliveries (of one event when `event_id` is given); return how many."""
        attempted = 0
        while attempted < limit and self._deliver_next(event_id):
            attempted += 1
        return attempted

    def _deliver_next(self, event_id: str | None = None) -> bool:
        now = self.clock()
        dead: tuple[Envelope, str, str] | None = None
        with self.db.transaction():
            row = self.db.one(_DUE_SQL, (now, event_id, event_id))
            if not row:
                return False
            envelope = Envelope(id=row["id"], type=row["type"], version=row["version"], occurred_at=row["occurred_at"],
                                aggregate=row["aggregate"], aggregate_id=row["aggregate_id"],
                                correlation_id=row["correlation_id"], causation_id=row["causation_id"],
                                payload=row["payload"])
            error = self._call(envelope, row["listener"])
            if error is None:
                self.db.run("UPDATE domain_event_deliveries SET status='done', attempts=attempts+1, delivered_at=%s,"
                            " last_error='' WHERE event_id=%s AND listener=%s", (now, row["event_id"], row["listener"]))
            else:
                attempts = row["attempts"] + 1
                if attempts >= self.max_attempts:
                    self.db.run("UPDATE domain_event_deliveries SET status='dead', attempts=%s, last_error=%s"
                                " WHERE event_id=%s AND listener=%s", (attempts, error, row["event_id"], row["listener"]))
                    dead = (envelope, row["listener"], error)
                else:
                    delay = min(MAX_BACKOFF_S, self.base_delay_s * 2 ** (attempts - 1))
                    self.db.run("UPDATE domain_event_deliveries SET attempts=%s, last_error=%s, next_attempt_at=%s"
                                " WHERE event_id=%s AND listener=%s",
                                (attempts, error, now + timedelta(seconds=delay), row["event_id"], row["listener"]))
        if dead and self.on_dead:
            try:
                self.on_dead(*dead)
            except Exception:  # noqa: BLE001 - the incident must not stop delivery of other events
                log.exception("dead-letter callback failed for %s", dead[1])
        return True

    def _call(self, envelope: Envelope, listener_name: str) -> str | None:
        """Run the listener in a savepoint; return None on success or the error text (its writes rolled back)."""
        listener = next((item for item in self.registry.listeners_for(envelope.type) if item.name == listener_name), None)
        if listener is None:
            return f"listener {listener_name} is not registered"
        try:
            with self.db.transaction():
                listener.handler(envelope, from_payload(envelope.type, envelope.payload))
        except Exception as exc:  # noqa: BLE001 - any listener failure is retried, then parked
            return f"{type(exc).__name__}: {str(exc)[:500]}"
        return None


def replay(db: Any, *, event_id: str | None = None, listener: str | None = None) -> int:
    """Put dead deliveries back to pending (optionally of one event or listener); return how many."""
    return db.run("UPDATE domain_event_deliveries SET status='pending', attempts=0, next_attempt_at=now(), last_error=''"
                  " WHERE status='dead' AND (%s::text IS NULL OR event_id=%s) AND (%s::text IS NULL OR listener=%s)",
                  (event_id, event_id, listener, listener))
