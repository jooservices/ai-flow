"""Request bodies of the fleet routes (moved out of the control app)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckProbeBody(BaseModel):
    project: str
    signature: str
    results: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class CheckProbeTokenBody(BaseModel):
    project: str
    repo: str


class ConfirmBody(BaseModel):
    model_config = {"extra": "forbid"}
    confirm: bool = False


class JoinBody(BaseModel):
    join_token: str
    name: str | None = Field(default=None, min_length=1, max_length=63)
    ip: str = ""


class JoinCommandBody(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=63)
    role: str = Field(default="", max_length=32)
    project: str = Field(default="", max_length=32)


class JoinInspectBody(BaseModel):
    join_token: str = Field(min_length=1, max_length=200)


class JoinTokenBody(BaseModel):
    provision_key: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=63)
    role: str = Field(default="worker", min_length=1, max_length=32)
    project: str = Field(min_length=1, max_length=32)


class ManagedAccountCreateBody(BaseModel):
    model_config = {"extra": "forbid"}
    provider: Literal["claude", "codex", "opencode", "grok"]
    name: str = Field(default="", max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$|^$")
    sessions: int = Field(ge=1, le=8)
    jobs: list[Literal["develop", "verify", "intake", "master"]] = Field(min_length=1, max_length=4)


class ManagedAccountSignInStatusBody(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: str = Field(min_length=1, max_length=64)
    status: Literal["starting", "waiting", "completed", "failed"]
    sign_in_url: str = Field(default="", max_length=2048)
    code: str = Field(default="", max_length=80)
    error: str = Field(default="", max_length=300)


class ManagedAccountUpdateBody(BaseModel):
    model_config = {"extra": "forbid"}
    sessions: int | None = Field(default=None, ge=1, le=8)
    jobs: list[Literal["develop", "verify", "intake", "master"]] | None = Field(default=None, min_length=1,
                                                                                  max_length=4)
    action: Literal["pause", "resume"] | None = None


class NodeOperationBody(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["install", "repair"]
    component: str = ""


class NodeOperationCheckpointBody(BaseModel):
    model_config = {"extra": "forbid"}
    rollback_envelope: dict[str, str]


class NodeOperationResultBody(BaseModel):
    status: Literal["completed", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
