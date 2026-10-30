"""Request bodies of the release routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel


class ReleaseFailureBody(BaseModel):
    release_version: str
    detail: str = ""


class WorktreeSweepBody(BaseModel):
    dirs: list[str] = []   # "<project>/<directory>" entries found under /srv/aiflow/work on the node
