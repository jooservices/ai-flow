"""Domain errors: what a service raises instead of an HTTP exception, mapped to a status in one place.

A service says what went wrong (`NotFound`, `Conflict`, `Forbidden`, `Invalid`, `Unauthorized`, `Unavailable`); this
module is the one place that knows the status code of each. They derive from FastAPI's `HTTPException`, so the existing
`except HTTPException` handlers and FastAPI's own error response keep working while services move off importing FastAPI:
a service imports only this module, and the response body stays `{"detail": message}`."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class DomainError(HTTPException):
    status_code_default = 500

    def __init__(self, detail: Any = "") -> None:
        super().__init__(status_code=self.status_code_default, detail=detail)


class Invalid(DomainError):
    status_code_default = 400


class Unauthorized(DomainError):
    status_code_default = 401


class Forbidden(DomainError):
    status_code_default = 403


class NotFound(DomainError):
    status_code_default = 404


class Conflict(DomainError):
    status_code_default = 409


class Unprocessable(DomainError):
    """The request is well formed but its values are not acceptable (HTTP 422)."""

    status_code_default = 422


class Unavailable(DomainError):
    status_code_default = 503
