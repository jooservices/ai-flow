"""HTTP routes for node-reported Flow Doctor evidence."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class OperatorInterventionBody(BaseModel):
    source_key: str = Field(min_length=1, max_length=240)
    source: Literal["shell", "configuration", "database", "jira", "system"]
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=120)
    target: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=1000)
    happened_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)
