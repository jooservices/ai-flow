"""A failing memory lookup in the work routes gives no related hits and logs a warning instead of failing the claim."""

from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.control.features.work_routes import routes


def _failing_related(*_args, **_kwargs):
    raise RuntimeError("memory store unavailable")


def test_related_hits_returns_no_hits_when_memory_lookup_raises(monkeypatch):
    monkeypatch.setattr(routes.memory, "related", _failing_related)
    runtime = SimpleNamespace(ctx=SimpleNamespace(db="db"), logger=SimpleNamespace(warning=lambda *_args: None))
    ticket = {"project": "SHOP", "summary": "Need help", "key": "SHOP-4"}

    assert routes._related_hits(runtime, ticket) == []


def test_related_hits_logs_a_warning_naming_the_ticket_when_memory_lookup_raises(monkeypatch):
    monkeypatch.setattr(routes.memory, "related", _failing_related)
    warnings = []
    runtime = SimpleNamespace(ctx=SimpleNamespace(db="db"),
                              logger=SimpleNamespace(warning=lambda *args: warnings.append(args)))
    ticket = {"project": "SHOP", "summary": "Need help", "key": "SHOP-4"}

    routes._related_hits(runtime, ticket)

    message, key, error = warnings[0]
    assert (len(warnings), message, key, str(error)) == (
        1, "memory lookup for %s failed: %s", "SHOP-4", "memory store unavailable")
