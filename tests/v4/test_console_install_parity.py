"""Characterization cases for helpers returned by the console route installer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.console import routes, routes_notifications

CASES = Path(__file__).with_name("fixtures") / "console-install-parity.jsonl"


class FakeApp:
    def __getattr__(self, method):
        return lambda *args, **kwargs: lambda handler: handler


@pytest.fixture
def installed_console_helpers():
    console_user = Mock(return_value="authorized")
    ctx = SimpleNamespace(repo=lambda: SimpleNamespace(projects={"ALPHA": object(), "BETA": object()}))
    deps = SimpleNamespace(console_user=console_user, optional_console_user=Mock(), require_capacity=Mock())
    return routes.install(FakeApp(), ctx, deps), console_user


def parity_cases(helper):
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line)["helper"] == helper]


def test_validate_project_keys_matches_recorded_cases(installed_console_helpers):
    helpers, _ = installed_console_helpers
    for case in parity_cases("validate_project_keys"):
        if "error" in case:
            with pytest.raises(HTTPException) as caught:
                helpers.validate_project_keys(case["input"])
            assert (caught.value.status_code, caught.value.detail) == (
                case["error"]["status"], case["error"]["detail"])
        else:
            assert helpers.validate_project_keys(case["input"]) == case["expected"]


def test_layout_page_matches_recorded_cases(installed_console_helpers):
    helpers, _ = installed_console_helpers
    for case in parity_cases("layout_page"):
        if "error" in case:
            with pytest.raises(HTTPException) as caught:
                helpers.layout_page(case["input"])
            assert (caught.value.status_code, caught.value.detail) == (
                case["error"]["status"], case["error"]["detail"])
        else:
            assert helpers.layout_page(case["input"]) == case["expected"]


def test_notification_admin_matches_recorded_call(installed_console_helpers):
    helpers, console_user = installed_console_helpers
    case = parity_cases("notification_admin")[0]
    assert helpers.notification_admin(object(), case["input"]["action"]) == case["expected"]["result"]
    args, kwargs = console_user.call_args
    assert args[1] == set(case["expected"]["permission"])
    assert kwargs == {key: value for key, value in case["expected"].items()
                      if key in {"target", "action"}}


def test_notification_json_matches_recorded_cases():
    for case in parity_cases("notification_json"):
        input_case = case["input"]
        async def json_body():
            if "raises" in input_case:
                error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                raise error if input_case["raises"] == "UnicodeDecodeError" else ValueError("invalid")
            return input_case["payload"]

        if "error" in case:
            with pytest.raises(HTTPException) as caught:
                asyncio.run(routes_notifications.notification_json(SimpleNamespace(json=json_body)))
            assert (caught.value.status_code, caught.value.detail) == (
                case["error"]["status"], case["error"]["detail"])
        else:
            result = asyncio.run(routes_notifications.notification_json(SimpleNamespace(json=json_body)))
            assert result == case["expected"]
