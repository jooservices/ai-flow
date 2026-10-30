"""Request bodies of the project_report routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel


class CloseProjectBody(BaseModel):
    actor: str = "owner"
