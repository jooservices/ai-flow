"""Pull request comments written by AI Flow (JAIF-416): one comment per kind, edited in place."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

EDITED_IN_PLACE = ("verify", "triage")


class PRCommentBody(BaseModel):
    run_id: str
    fence: int
    number: int
    body: str
    kind: str = ""  # "verify" or "triage": one comment of that kind per pull request, edited in place


def post(github: Any, repo: str, number: int, kind: str, body: str) -> str:
    """Write the comment: edit the one of this kind, or create a plain comment. Returns "created" or "updated"."""
    if kind in EDITED_IN_PLACE:
        return github.upsert_comment(repo, number, kind, body)
    github.comment(repo, number, body)
    return "created"
