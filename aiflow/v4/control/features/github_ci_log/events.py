"""Domain events published when the GitHub CI log route successfully reads a failing job log."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class GitHubCiLogRead(DomainEvent):
    type = "github.ci_log_read"
    owner = "github_ci_log"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    job: str
    sha: str
    lines: int

    def audit(self) -> Audit:
        return Audit("github", "ci-log", self.ticket, self.actor, self.job or "no failing job",
                     {"sha": self.sha, "lines": self.lines}, self.run_id, self.fence)
