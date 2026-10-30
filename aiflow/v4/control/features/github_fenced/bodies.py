"""Request bodies of the github_fenced routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel


class BranchCleanupBody(BaseModel):
    run_id: str
    fence: int
    branch: str


class GitHubTokenBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    branch: str = ""


class MergeBody(BaseModel):
    run_id: str
    fence: int
    number: int
    title: str = ""
    method: str = "squash"


class PRBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    head: str
    base: str
    title: str
    body: str = ""
    draft: bool = True


class ReviewerBody(BaseModel):
    comment: str = ""
    reason: str = ""
    question: str = ""
    answer: str = ""
