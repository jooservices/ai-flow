"""Characterize review-metrics CSV output across the github_fenced route split."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from aiflow.v4.control.features.github_fenced import routes
from aiflow.v4.control.features.github_fenced import routes_access
from aiflow.v4.control.features.github_fenced import routes_reviews


FIXTURE = Path(__file__).with_name("fixtures") / "github_fenced_install_parity.jsonl"


class RecordingApp:
    def __getattr__(self, _method):
        def register(_path="", **_options):
            return lambda handler: handler
        return register


def _case(name="review_metrics_csv"):
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    return next(row for row in rows if row["case"] == name)


def test_review_metrics_csv_matches_the_unchanged_route(monkeypatch):
    case = _case()

    class ReviewQueue:
        def __init__(self, *_args):
            self.item_kind = lambda _ticket: "review"
            self.current_item_kind = lambda _ticket: "review"
            self.thread = lambda _key: []
            self.data = lambda _project, _scope: {"metrics": case["metrics"]}

        def mount_human_reviews(self, *_args):
            pass

    monkeypatch.setattr(routes, "review_queue_api", SimpleNamespace(ReviewQueueAPI=ReviewQueue))
    deps = SimpleNamespace(
        audit=lambda *_args: None,
        console_user=lambda *_args: None,
        fenced=lambda *_args: None,
        finish_reviewer_run=lambda *_args: None,
        fleet_routes=SimpleNamespace(),
        matches_run_repository=lambda *_args: False,
        merge_command=lambda *_args: None,
        node_auth=lambda: "node",
        optional_console_user=lambda _request: None,
        projects_routes=SimpleNamespace(ticket_epic=lambda _ticket: ""),
        queued_merge=lambda *_args: None,
        record_reviewer_action=lambda *_args: None,
        repository_for_run=lambda *_args: "",
        reviewer_auth=lambda: "reviewer",
        reviewer_comment=lambda *_args: None,
        reviewer_labels=lambda *_args: None,
        reviewer_run=lambda *_args: None,
        reviewer_transition=lambda *_args: None,
        run_row=lambda *_args: None,
        settings_for_run=lambda *_args: None,
    )
    ctx = SimpleNamespace(settings=SimpleNamespace(console_login=False))
    installed = routes.install(RecordingApp(), ctx, deps)

    result = installed.export_review_metrics(
        SimpleNamespace(url=SimpleNamespace(path="/v1/reviews/export.csv")), case["project"])

    assert isinstance(result, PlainTextResponse)
    assert result.body.decode("utf-8") == case["body"]
    assert result.media_type == "text/csv"
    assert result.headers["content-disposition"] == "attachment; filename=human-review-metrics.csv"


def test_review_metrics_csv_helper_matches_the_recorded_route_output():
    case = _case()
    result = routes_reviews._review_metrics_csv(case["metrics"], case["project"])

    assert result.body.decode("utf-8") == case["body"]
    assert result.media_type == "text/csv"
    assert result.headers["content-disposition"] == "attachment; filename=human-review-metrics.csv"


def test_github_token_permission_helper_matches_recorded_cases(monkeypatch):
    for name in ("token_permissions_triage", "token_permissions_dev",
                 "token_permissions_version_release_disabled"):
        case = _case(name)
        monkeypatch.setattr(routes, "version_release",
                            SimpleNamespace(enabled=lambda _project, enabled=case["enabled"]: enabled))
        if "error" in case:
            with pytest.raises(HTTPException) as error:
                routes_access._github_token_permissions(case["run"], object())
            assert (error.value.status_code, error.value.detail) == (
                case["error"]["status"], case["error"]["detail"])
        else:
            assert routes_access._github_token_permissions(case["run"], object()) == case["result"]


def test_git_transfer_branch_validation_matches_recorded_cases():
    for name in ("token_branch_valid", "token_branch_invalid"):
        case = _case(name)
        if "error" in case:
            with pytest.raises(HTTPException) as error:
                routes_access._validate_git_transfer_branch(case["run"], case["branch"])
            assert (error.value.status_code, error.value.detail) == (
                case["error"]["status"], case["error"]["detail"])
        else:
            assert routes_access._validate_git_transfer_branch(case["run"], case["branch"]) is None


def test_git_proxy_upstream_matches_recorded_fakes(monkeypatch):
    for name in ("proxy_upstream_success", "proxy_push_target_refused"):
        _check_git_proxy_upstream_case(_case(name), monkeypatch)


def _check_git_proxy_upstream_case(case, monkeypatch):
    calls = []
    upstream = SimpleNamespace(**case.get("upstream", {}))

    async def body():
        return case.get("body", "").encode()

    request = SimpleNamespace(
        method=case["method"], headers=case.get("request_headers", {}), body=body,
        query_params=SimpleNamespace(multi_items=lambda: [tuple(item) for item in case.get("query", [])]))
    gateway = SimpleNamespace(kind="token", token=lambda _repo: ("upstream-token", None))
    runtime = SimpleNamespace(
        receive_pack_target=lambda _body: set(case.get("receive_targets", [])),
        github_gateway_for_connection=lambda *_args: calls.append("gateway") or gateway,
        log=SimpleNamespace(warning=lambda *_args: calls.append("warning")),
    )

    def upstream_call(*args):
        calls.append(args)
        return upstream

    monkeypatch.setattr(routes, "_github_git_upstream", upstream_call)
    if "error" in case:
        with pytest.raises(HTTPException) as error:
            asyncio.run(routes_access._github_git_proxy_upstream(
                object(), {"connection": case["connection"]}, case["repository"], case["suffix"],
                case["operation"], case["branch"], request, runtime))
        assert (error.value.status_code, error.value.detail) == (
            case["error"]["status"], case["error"]["detail"])
        assert calls == []
        return
    result = asyncio.run(routes_access._github_git_proxy_upstream(
        object(), {"connection": case["connection"]}, case["repository"], case["suffix"],
        case["operation"], case["branch"], request, runtime))
    assert result is upstream
    call = calls[-1]
    assert call[0:3] == (case["repository"], case["suffix"], case["method"])
    assert call[3] == [tuple(item) for item in case["query"]]
    assert call[4] == case["forwarded_headers"]
    assert call[6] == "upstream-token"
