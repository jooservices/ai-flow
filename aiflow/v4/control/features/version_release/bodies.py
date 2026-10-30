"""Request bodies of the version_release routes (moved out of the control app)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PostMergeCompleteBody(BaseModel):
    run_id: str
    fence: int
    head_sha: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    error: str = ""


class VersionReleaseClaimBody(BaseModel):
    project: str
    version_id: str
    repository: str = ""
    account: str


class VersionReleaseCompleteBody(BaseModel):
    run_id: str
    fence: int
    head_sha: str = ""
    source_sha: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
