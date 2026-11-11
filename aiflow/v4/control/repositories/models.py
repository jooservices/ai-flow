"""Frozen values returned by shared account repositories."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuotaRoutingRow:
    """Quota and risk inputs for one provider-routing account."""

    id: str
    provider: str
    quota: dict[str, Any] | None
    quota_at_risk: bool | None


@dataclass(frozen=True)
class CapacityRoutingRow:
    """Forecast inputs for one capacity-routing account."""

    id: str
    provider: str
    tier: str | None
    quota_forecast: dict[str, Any] | None
