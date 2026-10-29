"""Persistence for provider quota routing settings, state and assignment decisions."""
from __future__ import annotations

import json
from typing import Any


class ProviderRoutingRepository:
    """Own SQL for the provider-routing aggregate."""

    def __init__(self, db):
        self.db = db

    def settings_row(self) -> dict[str, Any] | None:
        return self.db.one("SELECT provider_order, ceilings, updated_by, updated_at FROM provider_routing_settings")

    def save_settings(self, order: list[str], ceilings: dict[str, int], by: str) -> None:
        self.db.run("INSERT INTO provider_routing_settings (id, provider_order, ceilings, updated_by)"
                    " VALUES (true,%s,%s::jsonb,%s) ON CONFLICT (id) DO UPDATE SET"
                    " provider_order=EXCLUDED.provider_order, ceilings=EXCLUDED.ceilings,"
                    " updated_by=EXCLUDED.updated_by, updated_at=now()",
                    (order, json.dumps(ceilings), by[:200]))

    def clear_state(self, providers: list[str]) -> None:
        if providers:
            self.db.run("DELETE FROM provider_routing_state WHERE provider=ANY(%s)", (providers,))

    def routing_states(self) -> dict[str, dict[str, Any]]:
        return {row["provider"]: row for row in self.db.all("SELECT * FROM provider_routing_state")}

    def hold_provider(self, provider: str, window: str, reset_at, used_pct: float) -> None:
        self.db.run("INSERT INTO provider_routing_state (provider, window_name, window_reset_at, used_pct)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT (provider) DO UPDATE SET"
                    " window_name=EXCLUDED.window_name, window_reset_at=EXCLUDED.window_reset_at,"
                    " used_pct=EXCLUDED.used_pct",
                    (provider, window, reset_at, used_pct))

    def release_provider(self, provider: str) -> None:
        self.db.run("DELETE FROM provider_routing_state WHERE provider=%s", (provider,))

    def record_decision(self, ticket: str, step: str, account: str, provider: str,
                        kind: str, reason: str) -> None:
        self.db.run("INSERT INTO routing_decisions (ticket_key, step, account_id, provider, kind, reason)"
                    " VALUES (%s,%s,%s,%s,%s,%s)", (ticket, step, account, provider, kind, reason[:500]))

    def decisions(self, limit: int, offset: int, ticket: str = "") -> list[dict[str, Any]]:
        where, args = ("WHERE ticket_key=%s", (ticket,)) if ticket else ("", ())
        rows = self.db.all(f"SELECT * FROM routing_decisions {where} ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
                           args + (limit, offset))
        return [{"ticket": row["ticket_key"], "step": row["step"], "account": row["account_id"],
                 "provider": row["provider"], "kind": row["kind"], "reason": row["reason"],
                 "at": row["at"].isoformat()} for row in rows]

    def decision_count(self, ticket: str = "") -> int:
        where, args = ("WHERE ticket_key=%s", (ticket,)) if ticket else ("", ())
        return int(self.db.one(f"SELECT count(*) AS n FROM routing_decisions {where}", args)["n"])
