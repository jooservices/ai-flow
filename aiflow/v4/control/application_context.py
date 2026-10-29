"""Typed dependencies shared by the v4 control API and its feature modules."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from ..core.controlrepo import ControlRepo
from ..core.fencing import Leases
from ..core.settings import Settings
from .db import DB
from .events import Events
from .github import GitHubApp
from .jira import Jira


RepositoryProvider = Callable[[], ControlRepo]


class Clock(Protocol):
    """Time source used by control modules and replaceable in tests."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    """Default UTC clock, matching the control API's existing time source."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class ApplicationContext:
    """Settings and runtime services shared with every control feature.

    ``db`` and ``repo`` keep the established handler API. ``database`` and
    ``repositories`` name the same dependencies explicitly for new modules.
    ``repo_configuration`` is the path of the source control-repository config.
    """

    settings: Settings
    db: DB
    leases: Leases
    events: Events
    repo: RepositoryProvider
    jira: Jira | None = None
    github: GitHubApp | None = None
    refresh_repo: Callable[[], None] | None = None
    jira_auth: object | None = None
    # Keep additions after the legacy fields so positional Ctx callers retain
    # the original (settings, db, leases, events, repo, jira, ...) ordering.
    repo_configuration: str = ""
    clock: Clock = field(default_factory=SystemClock)

    def __post_init__(self) -> None:
        if not callable(self.repo):
            raise TypeError("repositories must be callable")
        if not callable(getattr(self.clock, "now", None)):
            raise TypeError("clock must provide now()")
        if not self.repo_configuration:
            self.repo_configuration = self.settings.control_repo

    @property
    def database(self) -> DB:
        """The database dependency, under its explicit context name."""
        return self.db

    @property
    def repositories(self) -> RepositoryProvider:
        """The live control-repository configuration provider."""
        return self.repo

    @repositories.setter
    def repositories(self, provider: RepositoryProvider) -> None:
        if not callable(provider):
            raise TypeError("repositories must be callable")
        self.repo = provider


# Keep import and constructor compatibility for existing handlers and callers.
Ctx = ApplicationContext
