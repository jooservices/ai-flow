"""Request bodies of the work_routes routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel


class ClaimBody(BaseModel):
    ticket: str
    account: str
    step: str = "dev"
