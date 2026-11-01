"""Domain events published by the github_fenced routes when a fenced GitHub write succeeds.

A refused write is not an event: it keeps its `audit(..., accepted=False)` call in the route."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class GitHubTokenIssued(DomainEvent):
    type = "github.token_issued"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    expires: str

    def audit(self) -> Audit:
        return Audit("github", "token", self.ticket, self.actor, "issued",
                     {"repo": self.repo, "expires": self.expires}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubGitRelayTokenIssued(DomainEvent):
    type = "github.git_relay_token_issued"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    branch: str
    expires: str

    def audit(self) -> Audit:
        return Audit("github", "token", self.ticket, self.actor, "repository Git relay capability issued",
                     {"repo": self.repo, "branch": self.branch, "expires": self.expires, "transport": "control"},
                     self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubPrLookedUp(DomainEvent):
    type = "github.pr_lookup_completed"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    head: str
    result: str

    def audit(self) -> Audit:
        return Audit("github", "find_pr", self.ticket, self.actor, self.result,
                     {"repo": self.repo, "head": self.head}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubPrCreated(DomainEvent):
    type = "github.pr_created"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    url: str
    number: int | None
    draft: bool

    def audit(self) -> Audit:
        return Audit("github", "pr", self.ticket, self.actor, self.url,
                     {"number": self.number, "draft": self.draft, "repo": self.repo}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubCommentPosted(DomainEvent):
    type = "github.comment_posted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    number: int
    kind: str
    outcome: str

    def audit(self) -> Audit:
        return Audit("github", "comment", self.ticket, self.actor, "ok",
                     {"number": self.number, "repo": self.repo, "kind": self.kind, "outcome": self.outcome},
                     self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubBranchDeleted(DomainEvent):
    type = "github.branch_deleted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    branch: str
    deleted: bool

    def audit(self) -> Audit:
        return Audit("github", "delete-branch", self.ticket, self.actor,
                     "deleted" if self.deleted else "already gone",
                     {"branch": self.branch, "repo": self.repo}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubPushAccepted(DomainEvent):
    type = "github.push_accepted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    branch: str

    def audit(self) -> Audit:
        return Audit("github", "git-push", self.ticket, self.actor, "pushed",
                     {"repo": self.repo, "branch": self.branch}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubPrClosed(DomainEvent):
    type = "github.pr_closed"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    number: int

    def audit(self) -> Audit:
        return Audit("github", "close", self.ticket, self.actor, "pull request closed",
                     {"number": self.number, "repo": self.repo}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubBranchUpdated(DomainEvent):
    type = "github.branch_updated"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    repo: str
    number: int
    message: str

    def audit(self) -> Audit:
        return Audit("github", "update-branch", self.ticket, self.actor, self.message,
                     {"number": self.number, "repo": self.repo}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class ReviewerJiraCommentPosted(DomainEvent):
    type = "review.jira_comment_posted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    comment_id: str
    source: str
    chars: int

    def audit(self) -> Audit:
        return Audit("jira", "comment", self.ticket, self.actor, self.comment_id,
                     {"source": self.source, "chars": self.chars}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class ReviewerGitHubCommentPosted(DomainEvent):
    type = "review.github_comment_posted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    source: str
    number: int
    repo: str

    def audit(self) -> Audit:
        return Audit("github", "comment", self.ticket, self.actor, "ok",
                     {"source": self.source, "number": self.number, "repo": self.repo}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class ReviewerTicketTransitioned(DomainEvent):
    type = "review.ticket_transitioned"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    status: str
    target: str

    def audit(self) -> Audit:
        return Audit("jira", "transition", self.ticket, self.actor, self.status,
                     {"to": self.target, "source": "human-review"}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class ReviewerLabelsUpdated(DomainEvent):
    type = "review.labels_updated"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    add: list[str]
    remove: list[str]

    def audit(self) -> Audit:
        return Audit("jira", "fields", self.ticket, self.actor, "review labels updated",
                     {"source": "human-review", "add": self.add, "remove": self.remove}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class ReviewerActionAccepted(DomainEvent):
    type = "review.action_accepted"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    action: str
    kind: str
    text: str

    def audit(self) -> Audit:
        return Audit("human-review", self.action, self.ticket, "reviewer-console", "accepted",
                     {"kind": self.kind, "text": self.text}, self.run_id, self.fence)


@event
@dataclass(frozen=True)
class GitHubMergeCompleted(DomainEvent):
    type = "github.merge_completed"
    owner = "github_fenced"
    aggregate = "run"
    aggregate_field = "run_id"

    run_id: str
    fence: int
    ticket: str
    actor: str
    number: int
    method: str
    repo: str
    result: str

    def audit(self) -> Audit:
        return Audit("github", "merge", self.ticket, self.actor, self.result,
                     {"number": self.number, "method": self.method, "repo": self.repo}, self.run_id, self.fence)
