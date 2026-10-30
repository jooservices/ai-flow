"""Request bodies of the business routes (moved out of the control app)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChangeRequestBody(BaseModel):
    title: str = ""
    description: str = ""
    epic: str = ""


class StakeholderVerdictBody(BaseModel):
    change_key: str
    target_key: str = ""
    environment: Literal["develop", "production"]
    verdict: Literal["expected", "not_expected"]
    sender: str = Field(min_length=1, max_length=200)
    release_commit: str = Field(default="", max_length=500)
    expected: str = Field(default="", max_length=10_000)
    actual: str = Field(default="", max_length=10_000)
    screenshot_data: str = Field(default="", max_length=7_000_000)
    screenshot_filename: str = Field(default="screenshot.png", max_length=255)
