"""Dashboard read models and metrics served by the discovered dashboard router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from .. import analytics as run_analytics
from .. import incidents, merge_flow, quota as quota_reporting, route_deps
from ..features.review_rounds import repository as review_round_repository, service as review_rounds
from ..repositories import assumptions as assumptions_repo
from ..repositories import dashboard as dashboard_repository

router = APIRouter()


@router.get("/v1/dashboard/assumptions")
def dashboard_assumptions(request: Request, days: int = Query(14, ge=1, le=90)) -> dict:
    """Return assumptions made per day and how many of them were later contradicted."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    projects = list(user.project_keys) if user and user.project_keys is not None else None
    daily = assumptions_repo.assumptions_daily(ctx.db, days=days, projects=projects)
    recent = assumptions_repo.recent_assumptions(ctx.db, limit=20, projects=projects)
    total = sum(d["total"] for d in daily)
    contradicted = sum(d["contradicted"] for d in daily)
    return {
        "days": daily,
        "total_assumptions": total,
        "total_contradicted": contradicted,
        "recent": recent,
    }


@router.get("/v1/dashboard/review-rounds")
def dashboard_review_rounds(request: Request) -> dict:
    """Return merged-ticket review rounds and finding counts within the caller's Dashboard scope."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    projects = list(user.project_keys) if user and user.project_keys is not None else None
    return review_rounds.review_round_metrics(review_round_repository.ReviewRoundsRepository(ctx.db), projects)


def _accumulate_flow_failure(groups: dict[str, dict], run: dict, tracked: dict[str, dict]) -> bool:
    pattern = incidents.flow_error_pattern(run["error_kind"], run["summary"], run["step"], run["outcome"])
    if not pattern:
        return False
    signature, sample = pattern
    item = groups.setdefault(signature, {"signature": signature, "count": 0, "tickets": set(),
                                        "pattern": sample, "tokens": 0, "cost_usd": 0.0})
    item["count"] += 1
    item["tickets"].add(run["ticket_key"])
    item["tokens"] += int(run["tokens_in"] or 0) + int(run["tokens_out"] or 0)
    item["cost_usd"] += float(run["cost_usd"] or 0)
    return signature in tracked


def _flow_failure_row(item: dict, tracked: dict[str, dict]) -> dict:
    item["tickets"] = sorted(item["tickets"])
    tracker = tracked.get(item["signature"])
    item["bug"] = ({"ticket": tracker["ticket_key"], "state": tracker["state"],
                     "regressions": tracker["regression_count"]} if tracker else None)
    return item


def _flow_failure_groups(rows: list[dict], tracked: dict[str, dict], total_runs: int) -> dict:
    groups: dict[str, dict] = {}
    bugged_runs = sum(_accumulate_flow_failure(groups, run, tracked) for run in rows)
    items = [_flow_failure_row(item, tracked) for item in groups.values()]
    top = sorted((item for item in items if item["count"] >= 2),
                 key=lambda item: (-item["count"], -item["tokens"], item["signature"]))[:10]
    return {"window_hours": 24, "signatures": top, "runs_with_bug": bugged_runs,
            "total_runs": total_runs, "bugged_run_share": bugged_runs / total_runs if total_runs else 0.0}


@router.get("/v1/dashboard/waiting")
def dashboard_waiting(request: Request) -> dict:
    """Return dashboard-visible Human review ticket keys without exposing queue details."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    scope = user.project_keys if user and user.project_keys is not None else None
    return {"ticket_keys": request.app.state.review_queue.queue_keys(project_scope=scope)}


@router.get("/v1/dashboard/disagreements")
def dashboard_disagreements(request: Request) -> dict:
    """Tickets whose Jira state and pull request disagreed for more than five minutes (the flow keeps fixing them)."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    scope = list(user.project_keys) if user and user.project_keys is not None else None
    rows = merge_flow.disagreements(ctx.db, scope)
    return {"after_s": merge_flow.DISAGREEMENT_AFTER_S, "tickets": rows}


@router.get("/v1/dashboard/pipeline-failures")
def dashboard_pipeline_failures(request: Request, days: int = Query(30, ge=1, le=90)) -> dict:
    """First observed verify CI results per pull request, scoped to the caller's visible projects."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    project_keys = list(user.project_keys) if user and user.project_keys is not None else None
    return run_analytics.first_pipeline_failures(ctx.db, days, project_keys)


@router.get("/v1/dashboard/quota")
def dashboard_quota(request: Request) -> dict:
    """Return a dashboard-safe quota summary for accounts serving the caller's visible projects."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    scope = set(user.project_keys) if user and user.project_keys is not None else None
    repo = ctx.repo()
    node_rows, account_rows = dashboard_repository.quota_sources(ctx.db)
    nodes = {node["name"]: node for node in node_rows}
    quota_by_account = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}
    missing = [row for row in account_rows if not quota_by_account[str(row["id"])] and row.get("quota_source")]
    quota_by_account.update(quota_reporting.assessments(ctx.db, missing) if missing else {})

    labels = {"five_hour": "5 hours", "seven_day": "Weekly", "seven_day_opus": "Weekly Opus",
              "seven_day_sonnet": "Weekly Sonnet", "estimated_week": "Estimated weekly"}

    def percent(value: Any) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number:
            return None
        if 0 <= number <= 1:
            number *= 100
        return round(max(0, min(100, number)), 1)

    accounts = []
    for account in account_rows:
        configured = repo.account(account["id"])
        if configured is None:
            continue
        if scope is not None and not any(
                machine in nodes and scope.intersection(repo.projects_for_node(
                    machine, nodes[machine].get("projects") or [], bool(nodes[machine].get("dynamic"))))
                for machine in configured.machines):
            continue

        awareness_windows = (quota_by_account.get(account["id"]) or {}).get("windows") or []
        windows = [{
            "label": labels.get(str(window.get("name")), str(window.get("label") or "Quota window")),
            "used_pct": percent(window.get("used_pct")),
            "reset_at": window.get("reset_at"),
            "estimated": window.get("source") == "estimate",
        } for window in awareness_windows if isinstance(window, dict)]
        if not windows:
            raw_windows = (account.get("quota") or {}).get("windows") or {}
            windows = [{
                "label": labels.get(name, "Quota window"),
                "used_pct": percent(window.get("utilization", window.get("used_percent"))),
                "reset_at": window.get("resets_at") or window.get("reset_at"),
                "estimated": False,
            } for name, window in raw_windows.items() if isinstance(window, dict)]
        accounts.append({"provider": str(account.get("provider") or "unknown"), "windows": windows})

    provider_counts: dict[str, int] = {}
    rows = []
    for account in accounts:
        provider = account["provider"]
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        rows.append({"provider": provider, "label": f"Account {provider_counts[provider]}",
                     "windows": account["windows"]})
    return {"rows": rows}


@router.get("/v1/dashboard/flow-failures")
def dashboard_flow_failures(request: Request) -> dict:
    """Return the top repeating root-cause signatures within the caller's visible projects."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    scope = list(user.project_keys) if user and user.project_keys is not None else None
    rows = dashboard_repository.flow_failure_runs(ctx.db, scope)
    groups = {}
    for run in rows:
        pattern = incidents.flow_error_pattern(run["error_kind"], run["summary"], run["step"], run["outcome"])
        if pattern:
            groups[pattern[0]] = None
    signatures = sorted(groups)
    tracker_rows = dashboard_repository.flow_failure_trackers(ctx.db, signatures)
    tracked = {row["signature"]: row for row in tracker_rows}
    return _flow_failure_groups(rows, tracked, dashboard_repository.total_runs(ctx.db, scope))


@router.get("/v1/dashboard/yield")
def dashboard_yield(request: Request, days: int = Query(14, ge=1, le=90)) -> dict:
    """Return delivery-yield and verification-return metrics for the caller's visible projects."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    projects = list(user.project_keys) if user and user.project_keys is not None else None
    return run_analytics.yield_metrics(ctx.db, days, projects)


@router.get("/v1/dashboard/timeouts")
def dashboard_timeouts(request: Request, days: int = Query(30, ge=1, le=90)) -> dict:
    """Return developer-run timeout metrics for projects in the caller's Dashboard scope."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    projects = user.project_keys if user and user.project_keys is not None else None
    return run_analytics.timeouts(ctx.db, days, projects)
