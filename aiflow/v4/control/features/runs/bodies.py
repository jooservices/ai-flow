"""Request bodies of the runs routes (moved out of the control app)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FenceBody(BaseModel):
    fence: int
    progress: dict[str, Any] = Field(default_factory=dict)
    inbox_received: list[int] = Field(default_factory=list, max_length=100)
    inbox_included: list[int] = Field(default_factory=list, max_length=100)


class MasterActionBody(BaseModel):
    run_id: str
    fence: int
    action: str
    ticket: str = ""
    intent: str = ""
    category: str = ""  # infrastructure | secret | production_release: always escalated to the owner
    headline: str = "follow up"
    what: str = ""
    why: str = ""
    result: str = ""
    next: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    kind: str = "master_follow_up"
    subject: str = ""
    severity: str = "warning"
    title: str = ""
    detail: str = ""
    target_status: str = ""
    options: list[str] = Field(default_factory=list)
    observation_key: str = ""
    account_id: str = ""
    pause_minutes: int = 30
    target_run_id: str = ""
    signature: str = ""


class MasterBudgetBody(BaseModel):
    daily_budget_minutes: int = Field(ge=0, le=1440)


class MasterFlowBugsBody(BaseModel):
    run_id: str
    fence: int


class MasterPlanOutcomeBody(BaseModel):
    run_id: str
    fence: int
    valid: bool


class ResultBody(BaseModel):
    fence: int
    outcome: str
    error_kind: str = ""
    reset_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    branch: str = Field(default="", max_length=240)
    pr_url: str = ""
    head_sha: str = ""
    summary: str = ""
    quota: dict[str, Any] = Field(default_factory=dict)
    quota_observed_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)
