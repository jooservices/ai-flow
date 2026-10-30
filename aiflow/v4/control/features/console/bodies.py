"""Request bodies of the console routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel, Field


class ConsoleUserCreateBody(BaseModel):
    email: str
    display_name: str = ""
    roles: list[str]
    project_keys: list[str] | None = None


class ConsoleUserDisabledBody(BaseModel):
    disabled: bool


class ConsoleUserUpdateBody(BaseModel):
    roles: list[str]
    project_keys: list[str] | None = None


class LicenseKeyBody(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(min_length=1, max_length=2000)


class OfflineLicenseBody(BaseModel):
    model_config = {"extra": "forbid"}
    license_file: str = Field(min_length=1, max_length=50000)


class PlanLimitsBody(BaseModel):
    model_config = {"extra": "forbid"}
    plan: str
    limits: dict[str, int]


class ViewAsBody(BaseModel):
    role: str
