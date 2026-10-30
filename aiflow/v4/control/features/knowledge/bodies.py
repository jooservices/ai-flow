"""Request bodies of the knowledge routes (moved out of the control app)."""
from __future__ import annotations


from pydantic import BaseModel, Field


class KnowledgeApprovalBody(BaseModel):
    approved_by: str = "owner"


class KnowledgeBody(BaseModel):
    id: str = ""
    type: str
    title: str
    content: str
    actor: str
    archived: bool = False
    version: int | None = None


class KnowledgeItemActionBody(BaseModel):
    actor: str = Field(default="", max_length=200)
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="", max_length=16)
    epic: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=30_000)
    duplicate_id: int | None = None


class KnowledgeItemBody(BaseModel):
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="project", max_length=16)
    epic: str = Field(default="", max_length=100)
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=30_000)
    actor: str = Field(default="", max_length=200)


class KnowledgeRestoreBody(BaseModel):
    version: int
    actor: str
