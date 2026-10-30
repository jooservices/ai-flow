"""Request bodies of the backups routes (moved out of the control app)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BackupConfigBody(BaseModel):
    schedule: Literal["daily", "weekly"] = "daily"
    destination_kind: Literal["local", "mounted", "s3"] = "local"
    destination_path: str = "/srv/aiflow/backups"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_region: str = "us-east-1"
    retention_days: int = Field(default=30, ge=1, le=3650)
