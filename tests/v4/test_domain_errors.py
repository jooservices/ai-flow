"""Domain errors map to HTTP in one place and stay compatible with HTTPException handlers (JAIF-466)."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aiflow.v4.control.platform import errors


@pytest.mark.parametrize("cls,status", [(errors.Invalid, 400), (errors.Unauthorized, 401), (errors.Forbidden, 403),
                                        (errors.NotFound, 404), (errors.Conflict, 409), (errors.Unprocessable, 422),
                                        (errors.Unavailable, 503)])
def test_each_domain_error_has_its_status_and_is_an_http_exception(cls, status):
    error = cls("why")

    assert isinstance(error, HTTPException) and (error.status_code, error.detail) == (status, "why")


def test_a_route_raising_a_domain_error_answers_with_its_status_and_detail():
    app = FastAPI()

    @app.get("/x")
    def route():
        raise errors.Conflict("run is no longer active")

    response = TestClient(app).get("/x")

    assert (response.status_code, response.json()) == (409, {"detail": "run is no longer active"})


def test_services_that_moved_off_fastapi_raise_domain_errors():
    import ast
    from pathlib import Path

    source = Path(errors.__file__).parents[1] / "run_inbox.py"
    imported = {alias.name for node in ast.walk(ast.parse(source.read_text())) if isinstance(node, ast.ImportFrom)
                and node.module == "fastapi" for alias in node.names}

    assert imported == set()
