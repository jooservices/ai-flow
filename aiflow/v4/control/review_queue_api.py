"""Batch the reviewer-console reads and mount the review queue endpoint."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request

from . import review_metrics


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
        ctx = self.ctx

        @app.get("/v1/reviews")
        def human_reviews(request: Request, project: str = Query(default="")) -> dict:
            """Queue waiting items with their action set and project-filtered human-review metrics."""
            user = (console_user(request, "view.human_review") if ctx.settings.console_login
                    else optional_console_user(request))
            repo = ctx.repo()
            scope = user.project_keys if user and user.project_keys is not None else None
            if project and scope is not None and project not in scope:
                self.audit("console", "access_denied", "", "", None, user.subject, False,
                           "not available for your role", {"path": request.url.path, "project": project,
                                                              "roles": list(user.effective_roles)})
                raise HTTPException(403, "not available for your role")
            selected_keys = self.queue_keys(project, scope)
            metrics_data = self.data(project, scope, selected_keys)
            tickets = ctx.db.all("SELECT key, project, type, summary, description, labels, parent_key, status, blocked_by"
                                 " FROM tickets WHERE key=ANY(%s) ORDER BY project,key", (selected_keys,))
            needs_owner_keys = [str(ticket["key"]) for ticket in tickets if ticket["status"] == "Needs owner"]
            blocker_keys = sorted({str(key) for ticket in tickets for key in ticket.get("blocked_by") or []})
            blocker_statuses = {row["key"]: row["status"] for row in ctx.db.all(
                "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blocker_keys,))} if blocker_keys else {}
            epics = {row["key"]: row["status"] for row in ctx.db.all(
                "SELECT key, status FROM tickets WHERE key='TESTFLOW-5' AND type='Epic'")}
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
            if project:
                thread_event_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=%s)"
                thread_event_params = (project,)
                thread_run_filter = " AND project=%s"
                thread_run_params = (project,)
            elif scope is not None:
                thread_event_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))"
                thread_event_params = (list(scope),)
                thread_run_filter = " AND project=ANY(%s)"
                thread_run_params = (list(scope),)
            else:
                thread_event_filter, thread_event_params = "", ()
                thread_run_filter, thread_run_params = "", ()
            thread_dates: dict[str, Any] = {}
            for row in ctx.db.all("SELECT ticket,max(ts) AS latest FROM events WHERE type='review.action'"
                                  " AND data->>'action'='ask-ai'" + thread_event_filter + " GROUP BY ticket",
                                  thread_event_params):
                thread_dates[str(row["ticket"])] = row["latest"]
            for row in ctx.db.all("SELECT ticket_key AS ticket,max(started_at) AS latest FROM runs"
                                  " WHERE step IN ('dev','triage') AND outcome IN ('blocked','answered','escalated')"
                                  + thread_run_filter + " GROUP BY ticket_key", thread_run_params):
                key = str(row["ticket"])
                thread_dates[key] = max(thread_dates.get(key, row["latest"]), row["latest"])
            recent_thread_keys = sorted(thread_dates, key=lambda key: thread_dates[key], reverse=True)[:50]
            thread_keys = list(dict.fromkeys([*selected_keys, *recent_thread_keys]))
            thread_data = self.threads(thread_keys)
            thread_tickets = {str(ticket["key"]): ticket for ticket in tickets}
            missing_thread_tickets = [key for key in recent_thread_keys if key not in thread_tickets]
            if missing_thread_tickets:
                thread_tickets.update({str(ticket["key"]): ticket for ticket in ctx.db.all(
                    "SELECT key,project,summary,status FROM tickets WHERE key=ANY(%s)", (missing_thread_tickets,))})
            out = []
            for ticket in tickets:
                dev = dev_runs.get(ticket["key"])
                latest = latest_runs.get(ticket["key"])
                pr_url = (dev or {}).get("pr_url", "")
                match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
                project_config = repo.projects.get(ticket["project"])
                if dev and project_config:
                    repository = self.repository_for_run(dev, project_config)
                elif project_config:
                    repo_ticket = ({**ticket, "epic": self.ticket_epic(ticket)}
                                   if ticket.get("parent_key") else ticket)
                    repository = project_config.repo_for(repo_ticket)
                else:
                    repository = ""
                pr_detail: dict[str, Any] = {}
                if project_config and match:
                    try:
                        pr_detail = self.github_gateway(ctx, repository).pr(
                            repository.split("/", 1)[-1], int(match.group(1))) or {}
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning("could not load PR details for %s: %s", ticket["key"], exc)
                verify = verify_runs.get(ticket["key"])
                details = (verify or {}).get("details") or {}
                preview_url = ""
                preview = (project_config.environments or {}).get("preview") if project_config else None
                if isinstance(preview, dict):
                    preview_url = str(preview.get("url") or "")
                elif isinstance(preview, str):
                    preview_url = preview
                if ticket["project"] != "TESTFLOW" or epics.get("TESTFLOW-5") != "Done":
                    preview_url = ""
                open_blocker = any(str(blocker) not in blocker_statuses or
                                   blocker_statuses[str(blocker)] not in ("Done", "Closed")
                                   for blocker in (ticket.get("blocked_by") or []))
                kind = self.item_kind(
                    {**ticket, "_has_open_blocker": open_blocker}, latest,
                    (asked_by_key.get(ticket["key"]), triage_by_key.get(ticket["key"])))
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
                out.append({
                    **ticket,
                    "kind": kind,
                    "actions": actions,
                    "thread": thread_data.get(ticket["key"], []),
                    "repository": repository,
                    "jira_url": f"{ctx.settings.jira_site.rstrip('/')}/browse/{ticket['key']}",
                    "pr": {"url": pr_url, "number": int(match.group(1)) if match else None,
                           "title": pr_detail.get("title", ""),
                           "additions": pr_detail.get("additions"), "deletions": pr_detail.get("deletions"),
                           "changed_files": pr_detail.get("changed_files")},
                    "verify": {"outcome": (verify or {}).get("outcome", ""),
                               "summary": (verify or {}).get("summary", ""),
                               "verdict": details.get("verdict", ""), "checks": details.get("checks", ""),
                               "findings": details.get("findings", "")},
                    "preview_url": preview_url,
                })
            threads = []
            for key in recent_thread_keys:
                ticket = thread_tickets.get(key)
                thread = thread_data.get(key, [])
                if ticket and thread:
                    threads.append({**ticket, "thread": thread})
            return {"tickets": out, "threads": threads, **metrics_data}
