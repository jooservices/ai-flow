"""Request bodies of the projects routes (moved out of the control app)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EnvironmentRemovalBody(BaseModel):
    run_id: str
    fence: int
    removed: list[str] = Field(default_factory=list, max_length=50)
    needs: list[str] = Field(default_factory=list, max_length=20)


class GitHubConnectionTokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=600, repr=False)
    label: str = Field(default="", max_length=80)
    account: str = Field(default="", max_length=39)


class GitHubInstallationBody(BaseModel):
    installation_id: int = Field(ge=1)


class ProjectEnvironmentBody(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ProjectPauseBody(BaseModel):
    paused: bool = True


class ProjectPreflightBody(BaseModel):
    site: str = Field(min_length=1, max_length=300)
    project: str = Field(min_length=1, max_length=500)
    repo: str = Field(min_length=1, max_length=200)
    base: str = Field(default="develop", min_length=1, max_length=120)
    stack: str = Field(default="", max_length=80)
    containers: bool = False


class ProjectRepositoryConfigBody(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    base: str = Field(default="", max_length=120)
    checks: list[str] = Field(default_factory=list, max_length=50)
    stack: str = Field(default="", max_length=80)
    containers: bool | None = None
    connection: str = Field(default="", max_length=64)


class ProjectRepositoriesBody(BaseModel):
    repositories: list[ProjectRepositoryConfigBody] = Field(default_factory=list, max_length=100)
    epic_repositories: dict[str, str] = Field(default_factory=dict, max_length=500)


class ProjectServicesBody(BaseModel):
    services: list[str] = Field(default_factory=list, max_length=10)
