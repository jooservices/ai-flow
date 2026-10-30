"""Connect the event platform to a running control application without touching the app factory.

Each application context gets its own registry (so tests that build many apps never register a listener twice), filled
by the `register_listeners` function of every feature that has listeners. Publishing and the dispatcher loop go through here."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .catalog import DomainEvent, Envelope
from .dispatcher import Dispatcher
from .publisher import publish
from .registry import Registry

log = logging.getLogger("aiflow4.events")
DISPATCH_EVERY_S = 1.0
_LOCK = threading.Lock()


def _feature_installers():
    from ...features.tickets import listeners as tickets_listeners
    from ...features.escalation import listeners as escalation_listeners
    from . import audit_ledger
    # the audit ledger goes last: it registers one listener for every event type that asks to be audited
    from ...features.incidents import listeners as incident_listeners
    return (tickets_listeners.register_listeners, escalation_listeners.register_listeners,
            incident_listeners.register_listeners, audit_ledger.register_listeners)


def registry_for(ctx: Any) -> Registry:
    with _LOCK:
        registry = getattr(ctx, "event_registry", None)  # a context double may not declare the field
        if registry is None:
            registry = Registry()
            for register in _feature_installers():
                register(ctx, registry)
            ctx.event_registry = registry
        return registry


def publish_for(ctx: Any, item: DomainEvent, *, deliver_now: bool = False, **kwargs: str) -> Envelope:
    """Publish `item`. With `deliver_now`, its outbox listeners are attempted right away after the commit (a caller that
    answers an HTTP request and must show the reaction's result); a failure is left for the dispatcher to retry."""
    envelope = publish(ctx.db, item, registry=registry_for(ctx), **kwargs)
    if deliver_now:
        dispatcher_for(ctx).run_once(event_id=envelope.id)
    return envelope


def dispatcher_for(ctx: Any, **kwargs: Any) -> Dispatcher:
    return Dispatcher(ctx.db, registry_for(ctx), **kwargs)


def dispatch_loop(ctx: Any, stop: threading.Event) -> None:
    """Deliver outbox events every second until `stop`; one control replica does it at a time (a delivery is locked)."""
    dispatcher = dispatcher_for(ctx)
    while not stop.is_set():
        try:
            dispatcher.run_once()
        except Exception:  # noqa: BLE001 - the loop must survive a database blip
            log.exception("event dispatch failed")
        time.sleep(DISPATCH_EVERY_S)  # not stop.wait(): the loops' stop object is shared and a test double sets itself on wait
