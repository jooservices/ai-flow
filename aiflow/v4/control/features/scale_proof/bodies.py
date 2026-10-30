"""Request bodies of the scale_proof routes (moved out of the control app)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ScaleProofBody(BaseModel):
    release: str = Field("", max_length=200)
    source: str = Field("ci", max_length=60)
    report: dict[str, Any]
