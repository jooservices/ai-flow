"""Dashboard read models: waiting reviews, Jira/PR disagreements and the quota summary (moved out of api.py, JAIF-401)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .. import merge_flow, route_deps

router = APIRouter()


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


@router.get("/v1/dashboard/quota")
def dashboard_quota(request: Request) -> dict:
    """Return a dashboard-safe quota summary for accounts serving the caller's visible projects."""
    ctx = route_deps.context(request)
    user = (route_deps.console_user_for(request, "view.dashboard") if ctx.settings.console_login
            else route_deps.optional_console_user(ctx, request))
    scope = set(user.project_keys) if user and user.project_keys is not None else None
    repo = ctx.repo()
    nodes = {node["name"]: node for node in ctx.db.all(
        "SELECT name,projects,dynamic FROM nodes WHERE removed_at IS NULL")}
    account_rows = ctx.db.all("SELECT id,provider,quota,quota_forecast FROM accounts ORDER BY node,provider,id")
    quota_by_account = {str(row["id"]): row.get("quota_forecast") or {} for row in account_rows}

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
            "label": str(window.get("label") or labels.get(str(window.get("name")), "Quota window")),
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
