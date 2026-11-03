"""Batch the reviewer-console reads and mount the review queue endpoint."""
from __future__ import annotations

import logging
import re  # noqa: F401 - review_queue_mount resolves this module global at request time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: F401 - kept as module globals for the route

from . import review_metrics


@dataclass
class HumanReviewRows:
    tickets: list[dict]
    blocker_statuses: dict[str, str]
    epics: dict[str, str]
    dev_runs: dict[str, dict]
    latest_runs: dict[str, dict]
    verify_runs: dict[str, dict]
    asked_by_key: dict[str, dict]
    triage_by_key: dict[str, dict]


@dataclass
class HumanReviewThreads:
    data: dict[str, list[dict]]
    tickets: dict[str, dict]
    recent_keys: list[str]


def _human_review_thread_filters(project: str, scope: tuple[str, ...] | None) -> tuple[str, tuple, str, tuple]:
    if project:
        return (" AND ticket IN (SELECT key FROM tickets WHERE project=%s)", (project,),
                " AND project=%s", (project,))
    if scope is not None:
        return (" AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))", (list(scope),),
                " AND project=ANY(%s)", (list(scope),))
    return "", (), "", ()


def _human_review_actions(kind: str, match: Any, ticket: dict) -> list[str]:
    actions = {
        "review_before_merge": ["approve", "reject", "rework", "ask-ai", "skip-gate"],
        "question_from_ai": ["answer", "ask-ai"],
        "decision_needed": ["answer", "ask-ai"],
        "blocked": ["ask-ai"],
    }[kind]
    if not match:
        actions = [action for action in actions if action not in {"approve", "reject", "skip-gate"}]
    if kind == "review_before_merge" and "require-human" not in (ticket.get("labels") or []):
        actions = [action for action in actions if action != "skip-gate"]
    return actions


def _human_review_preview_url(ticket: dict, project_config: Any, epics: dict[str, str]) -> str:
    preview = (project_config.environments or {}).get("preview") if project_config else None
    if isinstance(preview, dict):
        preview_url = str(preview.get("url") or "")
    elif isinstance(preview, str):
        preview_url = preview
    else:
        preview_url = ""
    if ticket["project"] != "TESTFLOW" or epics.get("TESTFLOW-5") != "Done":
        return ""
    return preview_url


def _load_human_review_tickets(ctx: Any, selected_keys: list[str]) -> tuple[list[dict], list[str], dict[str, str], dict[str, str]]:
    tickets = ctx.db.all(
        "SELECT key, project, type, summary, description, labels, parent_key, status, blocked_by"
        " FROM tickets WHERE key=ANY(%s) ORDER BY project,key", (selected_keys,))
    needs_owner_keys = [str(ticket["key"]) for ticket in tickets if ticket["status"] == "Needs owner"]
    blocker_keys = sorted({str(key) for ticket in tickets for key in ticket.get("blocked_by") or []})
    blocker_statuses = {row["key"]: row["status"] for row in ctx.db.all(
        "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blocker_keys,))} if blocker_keys else {}
    epics = {row["key"]: row["status"] for row in ctx.db.all(
        "SELECT key, status FROM tickets WHERE key='TESTFLOW-5' AND type='Epic'")}
    return tickets, needs_owner_keys, blocker_statuses, epics


def _load_human_review_runs(ctx: Any, selected_keys: list[str], needs_owner_keys: list[str]) -> tuple[dict, ...]:
    if selected_keys:
        dev_runs = {str(row["ticket_key"]): row for row in ctx.db.all(
            "SELECT DISTINCT ON (ticket_key) ticket_key,pr_url,summary,details FROM runs"
            " WHERE ticket_key=ANY(%s) AND pr_url<>'' ORDER BY ticket_key,started_at DESC", (selected_keys,))}
        latest_runs = {str(row["ticket_key"]): row for row in ctx.db.all(
            "SELECT DISTINCT ON (ticket_key) ticket_key,step,outcome,summary,details,started_at FROM runs"
            " WHERE ticket_key=ANY(%s) ORDER BY ticket_key,started_at DESC", (selected_keys,))}
        verify_runs = {str(row["ticket_key"]): row for row in ctx.db.all(
            "SELECT DISTINCT ON (ticket_key) ticket_key,outcome,summary,details,started_at FROM runs"
            " WHERE ticket_key=ANY(%s) AND step='verify' ORDER BY ticket_key,started_at DESC", (selected_keys,))}
        if needs_owner_keys:
            asked_by_key = {str(row["ticket"]): row for row in ctx.db.all(
                "SELECT DISTINCT ON (ticket) ticket,ts FROM events WHERE ticket=ANY(%s)"
                " AND type='review.action' AND data->>'action'='ask-ai' ORDER BY ticket,ts DESC",
                (needs_owner_keys,))}
            triage_by_key = {str(row["ticket_key"]): row for row in ctx.db.all(
                "SELECT DISTINCT ON (ticket_key) ticket_key,started_at,status,outcome,ended_at FROM runs"
                " WHERE ticket_key=ANY(%s) AND step='triage' ORDER BY ticket_key,started_at DESC",
                (needs_owner_keys,))}
        else:
            asked_by_key, triage_by_key = {}, {}
    else:
        dev_runs, latest_runs, verify_runs, asked_by_key, triage_by_key = {}, {}, {}, {}, {}
    return dev_runs, latest_runs, verify_runs, asked_by_key, triage_by_key


def _load_human_review_rows(ctx: Any, selected_keys: list[str]) -> HumanReviewRows:
    tickets, needs_owner_keys, blocker_statuses, epics = _load_human_review_tickets(ctx, selected_keys)
    runs = _load_human_review_runs(ctx, selected_keys, needs_owner_keys)
    return HumanReviewRows(tickets, blocker_statuses, epics, *runs)


def _load_human_review_threads(queue: Any, ctx: Any, tickets: list[dict], selected_keys: list[str],
                               project: str, scope: tuple[str, ...] | None) -> HumanReviewThreads:
    event_filter, event_params, run_filter, run_params = _human_review_thread_filters(project, scope)
    thread_dates: dict[str, Any] = {}
    event_rows = ctx.db.all("SELECT ticket,max(ts) AS latest FROM events WHERE type='review.action'"
                            " AND data->>'action'='ask-ai'" + event_filter + " GROUP BY ticket", event_params)
    for row in event_rows:
        thread_dates[str(row["ticket"])] = row["latest"]
    run_rows = ctx.db.all("SELECT ticket_key AS ticket,max(started_at) AS latest FROM runs"
                          " WHERE step IN ('dev','triage') AND outcome IN ('blocked','answered','escalated')"
                          + run_filter + " GROUP BY ticket_key", run_params)
    for row in run_rows:
        key = str(row["ticket"])
        thread_dates[key] = max(thread_dates.get(key, row["latest"]), row["latest"])
    recent_keys = sorted(thread_dates, key=lambda key: thread_dates[key], reverse=True)[:50]
    thread_keys = list(dict.fromkeys([*selected_keys, *recent_keys]))
    thread_data = queue.threads(thread_keys)
    thread_tickets = {str(ticket["key"]): ticket for ticket in tickets}
    missing_keys = [key for key in recent_keys if key not in thread_tickets]
    if missing_keys:
        thread_tickets.update({str(ticket["key"]): ticket for ticket in ctx.db.all(
            "SELECT key,project,summary,status FROM tickets WHERE key=ANY(%s)", (missing_keys,))})
    return HumanReviewThreads(thread_data, thread_tickets, recent_keys)


def _human_review_repository_and_pr(queue: Any, ctx: Any, ticket: dict, dev: dict | None,
                                    project_config: Any) -> tuple[str, str, Any, dict[str, Any]]:
    pr_url = (dev or {}).get("pr_url", "")
    match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
    if dev and project_config:
        repository = queue.repository_for_run(dev, project_config)
    elif project_config:
        repo_ticket = ({**ticket, "epic": queue.ticket_epic(ticket)}
                       if ticket.get("parent_key") else ticket)
        repository = project_config.repo_for(repo_ticket)
    else:
        repository = ""
    pr_detail: dict[str, Any] = {}
    if project_config and match:
        try:
            pr_detail = queue.github_gateway(ctx, repository).pr(
                repository.split("/", 1)[-1], int(match.group(1))) or {}
        except Exception as exc:  # noqa: BLE001
            queue.log.warning("could not load PR details for %s: %s", ticket["key"], exc)
    return repository, pr_url, match, pr_detail


def _build_human_review_ticket(queue: Any, ctx: Any, repo: Any, ticket: dict, rows: HumanReviewRows,
                               thread_data: dict[str, list[dict]]) -> dict:
    dev = rows.dev_runs.get(ticket["key"])
    latest = rows.latest_runs.get(ticket["key"])
    project_config = repo.projects.get(ticket["project"])
    repository, pr_url, match, pr_detail = _human_review_repository_and_pr(
        queue, ctx, ticket, dev, project_config)
    verify = rows.verify_runs.get(ticket["key"])
    details = (verify or {}).get("details") or {}
    preview_url = _human_review_preview_url(ticket, project_config, rows.epics)
    open_blocker = any(str(blocker) not in rows.blocker_statuses
                       or rows.blocker_statuses[str(blocker)] not in ("Done", "Closed")
                       for blocker in (ticket.get("blocked_by") or []))
    kind = queue.item_kind({**ticket, "_has_open_blocker": open_blocker}, latest,
                           (rows.asked_by_key.get(ticket["key"]), rows.triage_by_key.get(ticket["key"])))
    actions = _human_review_actions(kind, match, ticket)
    return {
        **ticket,
        "kind": kind,
        "actions": actions,
        "thread": thread_data.get(ticket["key"], []),
        "repository": repository,
        "jira_url": f"{ctx.settings.jira_site.rstrip('/')}/browse/{ticket['key']}",
        "pr": {"url": pr_url, "number": int(match.group(1)) if match else None,
               "title": pr_detail.get("title", ""), "additions": pr_detail.get("additions"),
               "deletions": pr_detail.get("deletions"), "changed_files": pr_detail.get("changed_files")},
        "verify": {"outcome": (verify or {}).get("outcome", ""), "summary": (verify or {}).get("summary", ""),
                   "verdict": details.get("verdict", ""), "checks": details.get("checks", ""),
                   "findings": details.get("findings", "")},
        "preview_url": preview_url,
    }


def _build_recent_human_review_threads(rows: HumanReviewThreads) -> list[dict]:
    threads = []
    for key in rows.recent_keys:
        ticket = rows.tickets.get(key)
        thread = rows.data.get(key, [])
        if ticket and thread:
            threads.append({**ticket, "thread": thread})
    return threads


def _build_human_reviews_response(queue: Any, ctx: Any, repo: Any, project: str,
                                  scope: tuple[str, ...] | None) -> dict:
    selected_keys = queue.queue_keys(project, scope)
    metrics_data = queue.data(project, scope, selected_keys)
    rows = _load_human_review_rows(ctx, selected_keys)
    threads = _load_human_review_threads(queue, ctx, rows.tickets, selected_keys, project, scope)
    output = [_build_human_review_ticket(queue, ctx, repo, ticket, rows, threads.data)
              for ticket in rows.tickets]
    return {"tickets": output, "threads": _build_recent_human_review_threads(threads), **metrics_data}


class ReviewQueueAPI:
    def __init__(self, ctx, audit: Callable, repository_for_run: Callable, github_gateway: Callable,
                 ticket_epic: Callable, logger: logging.Logger):
        self.ctx = ctx
        self.audit = audit
        self.repository_for_run = repository_for_run
        self.github_gateway = github_gateway
        self.ticket_epic = ticket_epic
        self.log = logger

    def item_kind(self, ticket: dict, latest_run: dict | None = None,
                  question_context: tuple[dict | None, dict | None] | None = None) -> str:
        if ticket["status"] == "Human review":
            return "review_before_merge"
        if ticket["status"] == "Needs owner":
            if ticket.get("_has_open_blocker", bool(ticket.get("blocked_by"))):
                return "blocked"
            if "owner-escalated" in (ticket.get("labels") or []):
                return "decision_needed"
            if question_context is None:
                asked = self.ctx.db.one("SELECT ts FROM events WHERE ticket=%s AND type='review.action'"
                                        " AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1",
                                        (ticket["key"],))
                triaged = self.ctx.db.one(
                    "SELECT started_at,status,outcome,ended_at FROM runs WHERE ticket_key=%s AND step='triage'"
                    " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
            else:
                asked, triaged = question_context
            if asked and (not triaged or triaged["status"] == "running"
                          or triaged["outcome"] in ("failed", "timeout", "lost")
                          or (triaged["ended_at"] and asked["ts"] > triaged["ended_at"])):
                return "question_from_ai"
            if latest_run and latest_run.get("step") == "dev" and latest_run.get("outcome") == "blocked":
                return "question_from_ai"
            return "decision_needed"
        return "blocked"

    def current_item_kind(self, ticket: dict) -> str:
        """Classify a waiting ticket with the same live blocker check used by the review queue."""
        blockers = [str(blocker) for blocker in (ticket.get("blocked_by") or [])]
        statuses = {row["key"]: row["status"] for row in self.ctx.db.all(
            "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blockers,))} if blockers else {}
        has_open_blocker = any(statuses.get(blocker) not in ("Done", "Closed") for blocker in blockers)
        latest = self.ctx.db.one("SELECT step,outcome FROM runs WHERE ticket_key=%s"
                                 " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        return self.item_kind({**ticket, "_has_open_blocker": has_open_blocker}, latest)

    def threads(self, keys: list[str]) -> dict[str, list[dict]]:
        """Load question threads for a set of tickets without three queries per ticket."""
        if not keys:
            return {}
        threads: dict[str, list[dict]] = {}
        actions = self.ctx.db.all(
            "SELECT ticket,ts,data FROM (SELECT ticket,ts,data,row_number() OVER (PARTITION BY ticket ORDER BY ts DESC) AS n"
            " FROM events WHERE ticket=ANY(%s) AND type='review.action') recent WHERE n<=40 ORDER BY ticket,ts",
            (keys,))
        for row in actions:
            data = row.get("data") or {}
            action, text = str(data.get("action") or ""), str(data.get("text") or "")
            if action in {"ask-ai", "answer"} and text:
                threads.setdefault(str(row["ticket"]), []).append(
                    {"kind": "question" if action == "ask-ai" else "answer", "author": "Reviewer",
                     "text": text, "created_at": row["ts"].isoformat()})
        runs = self.ctx.db.all(
            "SELECT ticket_key,step,outcome,summary,details,started_at FROM ("
            "SELECT ticket_key,step,outcome,summary,details,started_at,row_number() OVER "
            "(PARTITION BY ticket_key,step ORDER BY started_at DESC) AS n FROM runs WHERE ticket_key=ANY(%s) AND "
            "((step='dev' AND outcome='blocked') OR (step='triage' AND outcome IN ('answered','escalated')))) recent "
            "WHERE n<=20 ORDER BY ticket_key,started_at", (keys,))
        for row in runs:
            if row["step"] == "dev":
                if row.get("summary"):
                    threads.setdefault(str(row["ticket_key"]), []).append(
                        {"kind": "question", "author": "AI", "text": row["summary"],
                         "created_at": row["started_at"].isoformat()})
                continue
            details = row.get("details") or {}
            answer = str(details.get("answer") or row.get("summary") or "")
            if row.get("outcome") == "escalated":
                answer = f"Could not answer safely: {details.get('why') or answer}"
            if answer:
                threads.setdefault(str(row["ticket_key"]), []).append(
                    {"kind": "ai_answer", "author": "AI", "text": answer,
                     "created_at": row["started_at"].isoformat()})
        return {key: sorted(thread, key=lambda item: item["created_at"]) for key, thread in threads.items()}

    def thread(self, key: str) -> list[dict]:
        return self.threads([key]).get(key, [])

    def data(self, project_filter: str = "", project_scope: tuple[str, ...] | None = None,
             selected_keys: list[str] | None = None) -> dict:
        repo = self.ctx.repo()
        configured = sorted(repo.projects)
        if project_scope is not None:
            configured = [key for key in configured if key in project_scope]
        if project_filter and project_filter not in configured:
            raise HTTPException(400, "unknown project filter")
        if project_filter:
            project_where, project_params = " AND project=%s", (project_filter,)
            ticket_filter, ticket_params = " AND ticket IN (SELECT key FROM tickets WHERE project=%s)", (project_filter,)
        elif project_scope is not None:
            project_where, project_params = " AND project=ANY(%s)", (list(project_scope),)
            ticket_filter, ticket_params = (" AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))",
                                            (list(project_scope),))
        else:
            project_where, project_params = "", ()
            ticket_filter, ticket_params = "", ()
        # Keep old run history only for the recent cohort, current waiters and their blockers.
        now = datetime.now(timezone.utc)
        cohort_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(weeks=8)
        current_keys = (selected_keys if selected_keys is not None
                        else self.queue_keys(project_filter, project_scope))
        metric_where = (" WHERE (coalesce(done_at,jira_updated,synced_at)>=%s"
                        " OR status IN ('Human review','Needs owner') OR cardinality(blocked_by)>0)" + project_where)
        metrics_tickets = self.ctx.db.all(
            "SELECT key, project, status, labels, blocked_by, done_at, jira_updated, synced_at FROM tickets"
            + metric_where, (cohort_start,) + project_params)
        metric_keys = {str(ticket["key"]) for ticket in metrics_tickets}
        blocker_keys = sorted({str(key) for ticket in metrics_tickets for key in ticket.get("blocked_by") or []}
                              - metric_keys)
        blocker_statuses = {str(row["key"]): str(row["status"]) for row in self.ctx.db.all(
            "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blocker_keys,))} if blocker_keys else {}
        events = self.ctx.db.all("SELECT ts,type,ticket,project,outcome,reason,data FROM events"
                                 " WHERE type IN ('jira.transition','review.action') AND (ts >= %s OR ticket=ANY(%s))"
                                 + ticket_filter + " ORDER BY ts", (cohort_start, current_keys) + ticket_params)
        relevant_keys = sorted(set(current_keys) | metric_keys
                               | {str(row["ticket"]) for row in events if row.get("ticket")})
        runs = self.ctx.db.all(
            "SELECT ticket_key,project,step,account_id,outcome,summary,details,started_at,ended_at FROM runs"
            " WHERE (started_at >= %s OR ticket_key=ANY(%s))" + project_where + " ORDER BY started_at",
            (cohort_start, relevant_keys) + project_params)
        audits = self.ctx.db.all("SELECT ts,ticket,action,target,accepted,result,payload FROM write_audit"
                                 " WHERE ticket=ANY(%s) AND (target='human-review' OR"
                                 " (target='jira' AND action='transition'))" + ticket_filter + " ORDER BY ts",
                                 (relevant_keys,) + ticket_params)
        metrics = review_metrics.calculate_review_metrics(metrics_tickets, events, runs, audits,
                                                          blocker_statuses=blocker_statuses)
        return {"metrics": metrics, "projects": configured}

    def queue_keys(self, project_filter: str = "", project_scope: tuple[str, ...] | None = None) -> list[str]:
        """Return review keys and unresolved dependency tickets in the requested project scope."""
        if project_filter:
            where, params = " AND project=%s", (project_filter,)
        elif project_scope is not None:
            where, params = " AND project=ANY(%s)", (list(project_scope),)
        else:
            where, params = "", ()
        candidates = self.ctx.db.all(
            "SELECT key, status, blocked_by FROM tickets WHERE (status IN ('Human review','Needs owner') OR"
            " (cardinality(blocked_by)>0 AND status NOT IN ('Done','Closed')))" + where + " ORDER BY project,key",
            params)
        blocker_keys = sorted({str(key) for ticket in candidates for key in ticket.get("blocked_by") or []})
        blocker_statuses = {row["key"]: row["status"] for row in self.ctx.db.all(
            "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blocker_keys,))} if blocker_keys else {}
        keys = []
        for ticket in candidates:
            if ticket["status"] in ("Human review", "Needs owner"):
                keys.append(ticket["key"])
            elif any(str(blocker) not in blocker_statuses or
                     blocker_statuses[str(blocker)] not in ("Done", "Closed")
                     for blocker in (ticket.get("blocked_by") or [])):
                keys.append(ticket["key"])
        return keys

    def mount_human_reviews(self, app: FastAPI, console_user: Callable, optional_console_user: Callable) -> None:
        from .review_queue_mount import install_human_reviews_route

        install_human_reviews_route(self, app, console_user, optional_console_user)
