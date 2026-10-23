"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import hashlib
import base64
import binascii
import hmac
import csv
import io
import ipaddress
import re
import logging
import os
import secrets
import shlex
import socket
import uuid
import html
import json
import httpx
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from urllib.parse import quote, urlsplit

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import Account, ControlRepo, epic_for_ticket
from ..core import backups as control_backups
from ..core.fencing import Leases
from ..core.settings import Settings
from ..core.services import service_gaps
from ..core.toolchains import STACK_TOOLS, missing_tools, pinned_version, toolchain_gaps, tools_for_stack
from .console_auth import (CANONICAL_ROLES, ConsoleUser, COOKIE_NAME, ROLE_LABELS, ROLE_PERMISSIONS, has_permission,
                           issue_session, landing_page, normalize_role, normalize_roles, role_satisfies, resolve_session_user,
                           verify_session)
from . import (business, escalations, jira_connect, knowledge, layouts, licensing, memory, role_health, rollout,
               support_bundle, tooling, version_release)
from . import review_metrics
from . import quota as quota_reporting
from . import projects as project_lifecycle
from .incidents import flow_bug_candidates, suggested_flow_fix
from .db import DB, J
from .events import Events
from .github import GitHubApp, GitHubError
from .github_connect import GitHubSecretStore, app_metadata
from .github_connections import DEFAULT_ID, Connection, GitHubConnections, GitHubTokenApp, verify_token
from .jira import Jira
from .policy import Decision, decide, decide_master_action, decide_read_only, explain
from .report import ReportNotFound, build_report, render_csv, render_html, render_markdown
from . import notify

log = logging.getLogger("aiflow4.api")
PRIORITY = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}
MASTER_SCOPE = "master:global"
MASTER_CYCLE_S = 60
DEFAULT_MASTER_DAILY_BUDGET_MINUTES = 60
RELEASES = Path("/srv/aiflow/control/releases")
_NON_ROUTABLE_JOIN_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))
# A ticket whose recent dev runs failed waits before the next try: 2, 4, 8, 16 then 30 minutes after the last
# failure (consecutive failures since the last run that produced work), so a broken ticket cannot spin.
BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM last l
  WHERE l.outcome IN ('failed', 'timeout', 'lost')
    AND NOT EXISTS (SELECT 1 FROM last g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND g.outcome NOT IN ('failed', 'timeout', 'lost'))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""
# An Intake ticket whose last intake run failed waits 10 minutes before the next refinement try (the same for the
# impact assessment of a change request).
INTAKE_BACKOFF_SQL = """
SELECT DISTINCT ON (ticket_key) ticket_key, outcome, ended_at FROM runs
WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
ORDER BY ticket_key, started_at DESC
"""

# `/v1/state` is consumed by the console and deployment tools. Its implementation is a shared helper in
# control/state.py, so keep its complete response contract explicit in OpenAPI for generated docs.
def _state_object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "additionalProperties": True,
            **({"required": list(required)} if required else {})}


def _state_array(items: dict) -> dict:
    return {"type": "array", "items": items}


def _state_nullable(value: dict) -> dict:
    return {"anyOf": [value, {"type": "null"}]}


_STATE_STRING = {"type": "string"}
_STATE_INTEGER = {"type": "integer"}
_STATE_NUMBER = {"type": "number"}
_STATE_BOOLEAN = {"type": "boolean"}
_STATE_DATETIME = {"type": "string", "format": "date-time"}
_STATE_ANY_OBJECT = _state_object({})
_STATE_STRING_ARRAY = _state_array(_STATE_STRING)
_STATE_RATE = _state_object({"tokens_per_hour": _STATE_NUMBER, "cost_usd_per_hour": _STATE_NUMBER,
                             "runs": _STATE_INTEGER, "steady_runs": _STATE_INTEGER},
                            ("tokens_per_hour", "cost_usd_per_hour", "runs", "steady_runs"))
_STATE_RUN = _state_object({
    "id": _STATE_STRING, "ticket": _STATE_STRING, "project": _STATE_STRING,
    "summary": _state_nullable(_STATE_STRING), "step": _STATE_STRING, "account": _STATE_STRING,
    "provider": _STATE_STRING, "model": _STATE_STRING, "node": _STATE_STRING, "fence": _STATE_INTEGER,
    "elapsed_s": _state_nullable(_STATE_INTEGER), "heartbeat_age_s": _state_nullable(_STATE_INTEGER),
    "lease_ms": _STATE_INTEGER, "progress": _STATE_ANY_OBJECT,
}, ("id", "ticket", "summary", "project", "step", "account", "provider", "model", "node", "fence",
    "elapsed_s", "heartbeat_age_s", "lease_ms", "progress"))
_STATE_RECENT_RUN = _state_object({
    "id": _STATE_STRING, "ticket": _STATE_STRING, "summary": _state_nullable(_STATE_STRING),
    "step": _STATE_STRING, "account": _STATE_STRING, "provider": _STATE_STRING, "model": _STATE_STRING,
    "node": _STATE_STRING, "outcome": _STATE_STRING, "error_kind": _STATE_STRING,
    "duration_s": _state_nullable(_STATE_INTEGER), "cost_usd": _STATE_NUMBER,
    "tokens_in": _STATE_INTEGER, "tokens_out": _STATE_INTEGER, "pr_url": _STATE_STRING,
    "ended_age_s": _state_nullable(_STATE_INTEGER),
}, ("id", "ticket", "summary", "step", "account", "provider", "model", "node", "outcome", "error_kind",
    "duration_s", "cost_usd", "tokens_in", "tokens_out", "pr_url", "ended_age_s"))
_STATE_ACCOUNT = _state_object({
    "id": _STATE_STRING, "provider": _STATE_STRING, "node": _STATE_STRING, "tier": _STATE_STRING,
    "roles": _STATE_STRING_ARRAY, "explicit_only": _STATE_BOOLEAN, "paused": _STATE_BOOLEAN,
    "jobs": _STATE_STRING_ARRAY, "sign_in_pending": _STATE_BOOLEAN, "sign_in_status": _STATE_STRING,
    "status": _STATE_STRING, "provider_status": _STATE_STRING,
    "cooldown": _state_nullable(_state_object({"reason": _STATE_STRING, "seconds_left": _STATE_INTEGER},
                                               ("reason", "seconds_left"))),
    "cooldown_until": _state_nullable(_STATE_DATETIME), "cooldown_since": _state_nullable(_STATE_DATETIME),
    "idle_since": _state_nullable(_STATE_DATETIME), "credential_expires_at": _STATE_STRING,
    "credential_expiry_warning": _STATE_BOOLEAN, "check_failed": _STATE_BOOLEAN,
    "quota": _STATE_ANY_OBJECT,
    "quota_awareness": _state_object({
        "source": _STATE_STRING, "is_estimate": _STATE_BOOLEAN, "runs_24h": _STATE_INTEGER,
        "confidence": _STATE_STRING, "rate_spread_ratio": _state_nullable(_STATE_NUMBER),
        "burn_rates": _state_object({"last_hour": _STATE_RATE, "last_day": _STATE_RATE,
                                     "whole_window": _STATE_RATE},
                                    ("last_hour", "last_day", "whole_window")),
        "usage_7d": _state_object({"tokens_in": _STATE_INTEGER, "tokens_out": _STATE_INTEGER,
                                   "tokens": _STATE_INTEGER, "cost_usd": _STATE_NUMBER,
                                   "runs": _STATE_INTEGER},
                                  ("tokens_in", "tokens_out", "tokens", "cost_usd", "runs")),
        "largest_run": _state_nullable(_state_object({"ticket": _state_nullable(_STATE_STRING),
            "model": _STATE_STRING, "tokens": _STATE_INTEGER, "context_tokens": _STATE_INTEGER,
            "cost_usd": _STATE_NUMBER, "duration_s": _STATE_NUMBER},
            ("ticket", "model", "tokens", "context_tokens", "cost_usd", "duration_s"))),
        "history": _state_array(_state_object({"day": _STATE_STRING, "tokens_in": _STATE_INTEGER,
            "tokens_out": _STATE_INTEGER, "tokens": _STATE_INTEGER, "cost_usd": _STATE_NUMBER,
            "runs": _STATE_INTEGER}, ("day", "tokens_in", "tokens_out", "tokens", "cost_usd", "runs"))),
        "windows": _state_array(_state_object({
            "name": _STATE_STRING, "label": _STATE_STRING, "source": _STATE_STRING,
            "used_pct": _state_nullable(_STATE_NUMBER), "left_pct": _state_nullable(_STATE_NUMBER),
            "used_tokens": _STATE_INTEGER, "left_tokens": _state_nullable(_STATE_INTEGER),
            "quota_tokens": _state_nullable(_STATE_NUMBER), "reset_at": _state_nullable(_STATE_DATETIME),
            "reset_source": _STATE_STRING, "tokens": _STATE_INTEGER, "cost_usd": _STATE_NUMBER,
            "burn_rates": _state_object({"last_hour": _STATE_RATE, "last_day": _STATE_RATE,
                                         "whole_window": _STATE_RATE},
                                        ("last_hour", "last_day", "whole_window")),
            "prediction": _state_object({"status": _STATE_STRING, "before_reset": _STATE_BOOLEAN,
                "confidence": _STATE_STRING, "exhaustion_at": _state_nullable(_STATE_DATETIME),
                "remaining_at_reset_pct": _state_nullable(_STATE_NUMBER),
                "remaining_now_pct": _state_nullable(_STATE_NUMBER)},
                ("status", "before_reset", "confidence", "exhaustion_at", "remaining_at_reset_pct",
                 "remaining_now_pct")),
        }, ("name", "label", "source", "used_pct", "left_pct", "used_tokens", "left_tokens",
            "quota_tokens", "reset_at", "reset_source", "tokens", "cost_usd", "burn_rates", "prediction"))),
        "at_risk": _STATE_BOOLEAN,
    }, ("source", "is_estimate", "runs_24h", "confidence", "rate_spread_ratio", "burn_rates", "usage_7d",
        "largest_run", "history", "windows", "at_risk")),
    "quota_used": _state_nullable(_STATE_NUMBER), "quota_resets_at": _state_nullable(_STATE_DATETIME),
    "tokens_today": _STATE_INTEGER, "cost_today": _STATE_NUMBER, "last_error": _STATE_STRING,
    "worker": _STATE_ANY_OBJECT, "model": _STATE_STRING, "effort": _state_nullable(_STATE_STRING),
    "runs_active": _STATE_INTEGER, "parallel": _STATE_INTEGER,
    "run": _state_nullable(_state_object({
        "id": _STATE_STRING, "ticket": _STATE_STRING, "summary": _state_nullable(_STATE_STRING),
        "step": _STATE_STRING, "elapsed_s": _state_nullable(_STATE_INTEGER), "fence": _STATE_INTEGER,
        "progress": _STATE_ANY_OBJECT,
    }, ("id", "ticket", "step", "elapsed_s", "fence", "progress"))),
}, ("id", "provider", "node", "tier", "roles", "explicit_only", "paused", "jobs", "sign_in_pending",
    "sign_in_status", "status", "provider_status", "cooldown", "cooldown_until", "cooldown_since", "idle_since",
    "credential_expires_at", "credential_expiry_warning", "check_failed", "quota", "quota_awareness", "quota_used",
    "quota_resets_at", "tokens_today", "cost_today", "last_error", "worker", "model", "effort", "runs_active",
    "parallel", "run"))
_STATE_INCIDENT = _state_object({
    "id": _STATE_INTEGER, "opened_at": _STATE_DATETIME, "closed_at": _state_nullable(_STATE_DATETIME),
    "severity": _STATE_STRING, "kind": _STATE_STRING, "subject": _STATE_STRING, "title": _STATE_STRING,
    "detail": _STATE_STRING,
}, ("id", "opened_at", "severity", "kind", "subject", "title", "detail"))
_STATE_READINESS_REASON = _state_object({"cause": _STATE_STRING, "ticket": _state_nullable(_STATE_STRING),
    "summary": _STATE_STRING, "status": _STATE_STRING, "bug": _STATE_STRING, "detail": _STATE_STRING},
    ("cause", "ticket", "summary"))
_STATE_RELEASE_STEP = _state_object({"step": _STATE_STRING, "state": _STATE_STRING, "run_id": _STATE_STRING,
    "node": _STATE_STRING, "detail": _STATE_STRING, "at": _state_nullable(_STATE_DATETIME), "url": _STATE_STRING},
    ("step", "state", "run_id", "node", "detail", "at"))

STATE_RESPONSE_SCHEMA = _state_object({
    "generated_at": _STATE_DATETIME, "env": _STATE_STRING, "control_repo": _STATE_STRING,
    "versions": {"type": "object", "additionalProperties": _STATE_STRING}, "version": _STATE_STRING,
    "links": _state_object({"o2": _STATE_STRING, "mail": _STATE_STRING, "gatus": _STATE_STRING},
                            ("o2", "mail", "gatus")),
    "tool_waits": _state_array(_state_object({
        "requirement": _STATE_STRING,
        "tickets": _state_array(_state_object({"key": _STATE_STRING, "summary": _STATE_STRING,
                                               "machine": _STATE_STRING}, ("key", "summary", "machine"))),
        "projects": _STATE_STRING_ARRAY, "machines": _STATE_STRING_ARRAY, "since": _STATE_DATETIME,
        "count": _STATE_INTEGER, "maintenance_ticket": _STATE_STRING,
    }, ("requirement", "tickets", "projects", "machines", "since", "count", "maintenance_ticket"))),
    "nodes": _state_array(_state_object({
        "name": _STATE_STRING, "ip": _STATE_STRING, "services": _state_array(_state_object({
            "name": _STATE_STRING, "projects": _STATE_STRING_ARRAY, "healthy": _STATE_BOOLEAN,
            "state": _STATE_STRING, "endpoint": _STATE_STRING, "image": _STATE_STRING, "detail": _STATE_STRING,
        }, ("name", "projects", "healthy", "state", "endpoint", "image", "detail"))),
        "role": _STATE_STRING, "status": _STATE_STRING, "accepting_work": _STATE_BOOLEAN,
        "sessions_busy": _STATE_INTEGER, "slots_total": _STATE_INTEGER, "slots_used": _STATE_INTEGER,
        "release": _STATE_STRING, "metrics": _STATE_ANY_OBJECT, "tools": _STATE_ANY_OBJECT,
        "has_agent": _STATE_BOOLEAN, "required_tools": _STATE_STRING_ARRAY,
        "desired_services": _state_array(_state_object({"name": _STATE_STRING, "unit": _STATE_STRING,
            "healthcheck_configured": _STATE_BOOLEAN}, ("name", "unit", "healthcheck_configured"))),
        "missing_tools": _STATE_STRING_ARRAY, "heartbeat_age_s": _state_nullable(_STATE_INTEGER),
        "accounts_assigned": _STATE_INTEGER, "in_control_repo": _STATE_BOOLEAN,
        "last_heartbeat": _state_nullable(_STATE_DATETIME), "idle_since": _state_nullable(_STATE_DATETIME),
        "purpose": _STATE_STRING, "notes": _STATE_STRING, "capabilities": _STATE_STRING_ARRAY,
        "projects": _STATE_STRING_ARRAY,
        "version_policy": _state_object({"compatible": _STATE_BOOLEAN, "state": _STATE_STRING,
                                          "reason": _STATE_STRING}, ("compatible", "state", "reason")),
        "joined_at": _STATE_DATETIME,
    }, ("name", "ip", "services", "role", "status", "accepting_work", "sessions_busy", "slots_total", "slots_used",
        "release", "metrics", "tools", "has_agent", "required_tools", "desired_services", "missing_tools",
        "heartbeat_age_s", "accounts_assigned", "in_control_repo", "last_heartbeat", "idle_since", "purpose", "notes",
        "capabilities", "projects", "version_policy", "joined_at"))),
    "accounts": _state_array(_STATE_ACCOUNT),
    "active_runs": _state_array(_STATE_RUN), "recent_runs": _state_array(_STATE_RECENT_RUN),
    "projects": _state_array(_state_object({
        "key": _STATE_STRING, "repo": _STATE_STRING, "repositories": _STATE_STRING_ARRAY,
        "base": _STATE_STRING, "environments": _STATE_ANY_OBJECT,
        "work_minutes": _state_object({"target": _STATE_INTEGER, "warn": _STATE_INTEGER, "hard": _STATE_INTEGER},
                                       ("target", "warn", "hard")),
        "counts": {"type": "object", "additionalProperties": _STATE_INTEGER},
        "status": _STATE_STRING, "closed": _STATE_BOOLEAN, "closed_at": _state_nullable(_STATE_DATETIME),
        "closed_by": _STATE_STRING, "report_version": _STATE_INTEGER, "close_suggested": _STATE_BOOLEAN,
    }, ("key", "repo", "repositories", "base", "environments", "work_minutes", "counts", "status", "closed",
        "closed_at", "closed_by", "report_version", "close_suggested"))),
    "tickets": _state_array(_state_object({
        "key": _STATE_STRING, "project": _STATE_STRING, "status": _STATE_STRING, "type": _STATE_STRING,
        "priority": _STATE_STRING, "labels": _STATE_STRING_ARRAY, "summary": _STATE_STRING,
        "parent_key": _state_nullable(_STATE_STRING), "blocked_by": _STATE_STRING_ARRAY,
        "epic": _STATE_STRING, "repository": _STATE_STRING, "run": _state_nullable(_STATE_RUN),
        "last_run": _state_nullable(_STATE_RECENT_RUN), "updated_age_s": _state_nullable(_STATE_INTEGER),
    }, ("key", "project", "status", "type", "priority", "labels", "summary", "parent_key", "blocked_by", "epic",
        "repository", "run", "last_run", "updated_age_s"))),
    "release_readiness": _state_array(_state_object({
        "project": _STATE_STRING, "version_id": _STATE_STRING, "version": _STATE_STRING, "ready": _STATE_BOOLEAN,
        "ticket_count": _STATE_INTEGER, "done_count": _STATE_INTEGER, "tickets": _STATE_STRING_ARRAY,
        "reasons": _state_array(_STATE_READINESS_REASON),
    }, ("project", "version_id", "version", "ready", "ticket_count", "done_count", "tickets", "reasons"))),
    "release_progress": _state_array(_state_object({
        "project": _STATE_STRING, "version_id": _STATE_STRING, "version": _STATE_STRING, "released": _STATE_BOOLEAN,
        "release_date": _state_nullable(_STATE_STRING), "ready": _STATE_BOOLEAN, "ticket_count": _STATE_INTEGER,
        "done_count": _STATE_INTEGER, "started_at": _state_nullable(_STATE_DATETIME),
        "tickets": _state_array(_state_object({"key": _STATE_STRING, "summary": _STATE_STRING,
            "status": _STATE_STRING, "added_at": _state_nullable(_STATE_DATETIME), "added_late": _STATE_BOOLEAN},
            ("key", "summary", "status", "added_at", "added_late"))),
        "open_tickets": _state_array(_state_object({"key": _STATE_STRING, "summary": _STATE_STRING,
            "status": _STATE_STRING, "added_at": _state_nullable(_STATE_DATETIME), "added_late": _STATE_BOOLEAN},
            ("key", "summary", "status", "added_at", "added_late"))),
        "late_tickets": _state_array(_state_object({"key": _STATE_STRING, "summary": _STATE_STRING,
            "status": _STATE_STRING, "added_at": _state_nullable(_STATE_DATETIME), "added_late": _STATE_BOOLEAN},
            ("key", "summary", "status", "added_at", "added_late"))),
        "reasons": _state_array(_STATE_READINESS_REASON),
        "release": _state_object({"status": _STATE_STRING, "tracker": _STATE_STRING, "tag": _STATE_STRING,
            "github_release_url": _STATE_STRING, "detail": _STATE_STRING, "steps": _state_array(_STATE_RELEASE_STEP)},
            ("status", "tracker", "tag", "github_release_url", "detail", "steps")),
    }, ("project", "version_id", "version", "released", "release_date", "ready", "ticket_count", "done_count",
        "started_at", "tickets", "open_tickets", "late_tickets", "reasons", "release"))),
    "incidents": _state_array(_STATE_INCIDENT), "incidents_recent": _state_array(_STATE_INCIDENT),
    "events": _state_array(_state_object({
        "ts": _STATE_DATETIME, "type": _STATE_STRING, "ticket": _STATE_STRING, "account": _STATE_STRING,
        "node": _STATE_STRING, "outcome": _STATE_STRING, "reason": _STATE_STRING,
    }, ("ts", "type", "ticket", "account", "node", "outcome", "reason"))),
    "sa": _state_object({
        "thresholds": _state_object({"slots_used_percent": _STATE_NUMBER, "slots_duration_minutes": _STATE_NUMBER,
            "role_cooldown_minutes": _STATE_NUMBER, "idle_days": _STATE_NUMBER,
            "credential_expiry_days": _STATE_NUMBER},
            ("slots_used_percent", "slots_duration_minutes", "role_cooldown_minutes", "idle_days", "credential_expiry_days")),
        "slots": _state_object({"used": _STATE_INTEGER, "total": _STATE_INTEGER,
            "utilization_percent": _state_nullable(_STATE_NUMBER), "pressure_since": _state_nullable(_STATE_DATETIME)},
            ("used", "total", "utilization_percent", "pressure_since")),
        "recommendations": _state_array(_state_object({"kind": _STATE_STRING, "subject": _STATE_STRING,
            "reason": _STATE_STRING}, ("kind", "subject", "reason"))),
        "policy_denials": _state_array(_state_object({"id": _STATE_INTEGER, "ts": _STATE_DATETIME,
            "ticket": _STATE_STRING, "account": _STATE_STRING, "node": _STATE_STRING, "run_id": _STATE_STRING,
            "reason": _STATE_STRING, "command": _state_nullable(_STATE_STRING)},
            ("id", "ts", "ticket", "account", "node", "run_id",
                "reason", "command"))),
        "provider_blocks": _state_array(_STATE_ACCOUNT),
        "network_issues": _state_array(_state_object({"source": _STATE_STRING,
            "id": {"anyOf": [_STATE_INTEGER, _STATE_STRING]},
            "ts": _STATE_DATETIME, "type": _STATE_STRING, "ticket": _STATE_STRING, "project": _STATE_STRING,
            "step": _STATE_STRING, "account": _STATE_STRING, "node": _STATE_STRING, "run_id": _STATE_STRING,
            "status": _STATE_STRING, "outcome": _STATE_STRING, "error_kind": _STATE_STRING,
            "detail": _STATE_STRING, "attempts": _STATE_INTEGER, "retry": _STATE_BOOLEAN,
            "reason": _STATE_STRING, "data": _STATE_ANY_OBJECT}, ("source",))),
    }, ("thresholds", "slots", "recommendations", "policy_denials", "provider_blocks", "network_issues")),
    "role_health": _state_object({
        "settings": _state_object({"window_runs": _STATE_INTEGER, "failure_percent": _STATE_NUMBER,
            "updated_by": _STATE_STRING, "updated_at": _state_nullable(_STATE_DATETIME),
            "defaults": _state_object({"window_runs": _STATE_INTEGER, "failure_percent": _STATE_INTEGER},
                                       ("window_runs", "failure_percent"))},
            ("window_runs", "failure_percent", "updated_by", "updated_at", "defaults")),
        "metrics_days": _STATE_INTEGER,
        "roles": _state_array(_state_object({
            "account": _STATE_STRING, "provider": _STATE_STRING, "role": _STATE_STRING, "enabled": _STATE_BOOLEAN,
            "mode": _STATE_STRING, "note": _STATE_STRING, "disabled_at": _state_nullable(_STATE_DATETIME),
            "failure_score": _state_nullable(_STATE_NUMBER), "scored_runs": _STATE_INTEGER,
            "success_rate": _state_nullable(_STATE_NUMBER), "finished_tickets": _STATE_INTEGER,
            "cost_per_finished_ticket": _state_nullable(_STATE_NUMBER), "rework_rounds": _STATE_INTEGER,
            "probation": _state_nullable(_state_object({"open_ticket": _STATE_STRING, "streak": _STATE_INTEGER,
                "needed": _STATE_INTEGER, "results": _STATE_STRING_ARRAY}, ("open_ticket", "streak", "needed", "results"))),
        }, ("account", "provider", "role", "enabled", "mode", "note", "disabled_at", "failure_score", "scored_runs",
            "success_rate", "finished_tickets", "cost_per_finished_ticket", "rework_rounds", "probation"))),
    }, ("settings", "metrics_days", "roles")),
    "rollout": _state_nullable(_state_object({
        "id": _STATE_INTEGER, "target": _STATE_STRING, "previous": _STATE_STRING, "status": _STATE_STRING,
        "wave": _STATE_INTEGER, "batch_size": _STATE_INTEGER, "failed_node": _STATE_STRING,
        "reason": _STATE_STRING, "rollback": _STATE_STRING, "started_at": _STATE_DATETIME,
        "nodes": _state_array(_state_object({"node": _STATE_STRING, "wave": _STATE_INTEGER,
            "status": _STATE_STRING, "from": _STATE_STRING, "detail": _STATE_STRING},
            ("node", "wave", "status", "from", "detail"))),
    }, ("id", "target", "previous", "status", "wave", "batch_size", "failed_node", "reason", "rollback",
        "started_at", "nodes"))),
    "version_policy": _state_object({"control": _STATE_STRING, "max_minor_lag": _STATE_INTEGER},
                                     ("control", "max_minor_lag")),
    "totals": _state_object({"runs_24h": _STATE_INTEGER, "cost_24h": _STATE_NUMBER,
        "tokens_in_24h": _STATE_INTEGER, "tokens_out_24h": _STATE_INTEGER,
        "done_24h": _STATE_INTEGER, "failed_24h": _STATE_INTEGER},
        ("runs_24h", "cost_24h", "tokens_in_24h", "tokens_out_24h", "done_24h", "failed_24h")),
}, ("generated_at", "env", "control_repo", "versions", "version", "links", "tool_waits", "nodes", "accounts",
    "active_runs", "recent_runs", "projects", "tickets", "release_readiness", "release_progress", "incidents",
    "incidents_recent", "events", "sa", "role_health", "rollout", "version_policy", "totals"))


def h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def valid_control_host(value: str) -> bool:
    """Accept only a DNS name or IP literal, optionally followed by a TCP port."""
    if not value or any(char.isspace() or ord(char) < 0x21 or ord(char) > 0x7e for char in value):
        return False

    port_text = ""
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?", value)
        if not match:
            return False
        try:
            if ipaddress.ip_address(match.group(1)).version != 6:
                return False
        except ValueError:
            return False
        port_text = match.group(2) or ""
    else:
        if value.count(":") > 1:
            return False
        hostname, separator, port_text = value.partition(":")
        if separator and not re.fullmatch(r"[0-9]{1,5}", port_text):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if not re.fullmatch(
                r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?",
                hostname,
            ):
                return False
        else:
            if address.version != 4:
                return False

    return not port_text or 1 <= int(port_text) <= 65535


def validated_join_control_url(value: str) -> str:
    """Validate the operator-configured API address advertised to a new machine."""
    control = str(value or "").strip()
    if not control or "\\" in control or any(ord(char) < 32 or char.isspace() for char in control):
        raise ValueError("join control URL is not configured")
    try:
        parsed = urlsplit(control)
        host = parsed.hostname or ""
        # Accessing .port validates malformed ports as well.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("join control URL is invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or "?" in control or "#" in control):
        raise ValueError("join control URL must be an HTTP(S) URL without credentials, query, or fragment")
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise ValueError("join control URL must not point to localhost")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            # Some URL stacks accept abbreviated or integer-form IPv4 literals; normalize those before checking.
            address = ipaddress.ip_address(socket.inet_aton(normalized_host))
        except (OSError, ValueError):
            address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address and (address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast
                    or address.is_reserved or any(address.version == net.version and address in net
                                                  for net in _NON_ROUTABLE_JOIN_NETWORKS)):
        raise ValueError("join control URL must use an address reachable from joining nodes")
    return control.rstrip("/")


def join_bootstrap_command(token: str, control: str) -> str:
    """Return the documented single-command bootstrap for a five-minute, node-bound join token."""
    installer = shlex.quote(f"{control.rstrip('/')}/v1/install.sh")
    return f"curl -fsSL {installer} | sudo sh -s -- --token {shlex.quote(token)}"


@dataclass
class Ctx:
    settings: Settings
    db: DB
    leases: Leases
    events: Events
    repo: Callable[[], ControlRepo]
    jira: Jira | None = None
    github: GitHubApp | None = None
    refresh_repo: Callable[[], None] | None = None
    jira_auth: Any = None  # jira_connect.JiraAuth: the Jira credential connected on the Integrations screen


github_connection_used: ContextVar[str] = ContextVar("github_connection_used", default="")


def github_connections(ctx: Ctx) -> GitHubConnections:
    """The registry of the connections after the default one, shared by every API replica through the secret store."""
    registry = getattr(ctx, "github_registry", None)
    if registry is None or registry.root != Path(ctx.settings.github_app_dir):
        registry = GitHubConnections(ctx.settings.github_app_dir, lambda directory, org: GitHubApp(directory, org),
                                     lambda token, account, kind: GitHubTokenApp(token, account, kind))
        ctx.github_registry = registry
    return registry


def github_connection_list(ctx: Ctx) -> list[Connection]:
    """Every connection in order, the legacy single App first."""
    out: list[Connection] = []
    default = github_gateway(ctx)
    if default is not None:
        meta = getattr(default, "meta", {}) or {}
        out.append(Connection(DEFAULT_ID, str(meta.get("name") or "GitHub App"), "app",
                              str(getattr(default, "org", "") or ctx.settings.github_org),
                              str(meta.get("account_type") or ""), default))
    return out + github_connections(ctx).connections()


def github_route(ctx: Ctx, repo: str) -> Connection:
    """The connection that serves a repository: its assignment, else the first of its owner that reaches it.

    Refuses with 409 when the assigned connection is gone or cannot reach the repository, and when none can."""
    registry = github_connections(ctx)
    connections = github_connection_list(ctx)
    if not connections:
        raise HTTPException(503, "GitHub is not connected")
    owner = repo.split("/", 1)[0].lower() if "/" in repo else ""
    assigned = registry.assignments().get(repo.lower())
    if assigned:
        chosen = next((c for c in connections if c.id == assigned), None)
        if chosen is None or not registry.reaches(chosen.id, chosen.gateway, repo):
            raise HTTPException(409, f"the GitHub connection assigned to {repo} cannot reach it")
        return chosen
    for conn in connections:
        if (not owner or conn.account.lower() == owner) and registry.reaches(conn.id, conn.gateway, repo):
            return conn
    raise HTTPException(409, f"no GitHub connection can reach {repo}")


def github_gateway(ctx: Ctx, repo: str | None = None) -> GitHubApp | None:
    """Load the shared secret-store configuration lazily and refresh it across API replicas.

    With a repository, always check that the selected connection can reach it, including a single legacy App.
    The connection used is remembered for the audit log."""
    if repo is not None:
        conn = github_route(ctx, repo)
        github_connection_used.set(conn.id)
        return conn.gateway
    gateway = ctx.github
    if gateway is None:
        store = GitHubSecretStore(ctx.settings.github_app_dir)
        if store.active_exists:
            gateway = GitHubApp(ctx.settings.github_app_dir, ctx.settings.github_org)
            ctx.github = gateway
    elif hasattr(gateway, "_refresh_config"):
        gateway._refresh_config()
        if not getattr(gateway, "configured", True):
            ctx.github = None
            gateway = None
    if gateway is not None and getattr(gateway, "org", ""):
        ctx.settings.github_org = str(gateway.org)
    github_connection_used.set(DEFAULT_ID if gateway is not None else "")
    return gateway


def github_gateway_for_connection(ctx: Ctx, connection_id: str, repo: str) -> GitHubApp:
    """Resolve the connection captured by a run and confirm it still reaches the repository."""
    registry = github_connections(ctx)
    connection = next((item for item in github_connection_list(ctx) if item.id == connection_id), None)
    if connection is None or not registry.reaches(connection.id, connection.gateway, repo):
        raise HTTPException(409, f"the GitHub connection assigned to {repo} cannot reach it")
    github_connection_used.set(connection.id)
    return connection.gateway


def github_control_gateway(ctx: Ctx) -> GitHubApp:
    """Resolve the connection assigned to the control repository, preserving per-repository routing."""
    repository = f"{ctx.settings.github_org}/{Path(ctx.settings.control_repo).name}"
    gateway = github_gateway(ctx, repository)
    if gateway is None:
        raise HTTPException(503, "GitHub is not connected")
    return gateway


def github_context_for_repository(ctx: Ctx, repo: str) -> tuple[GitHubApp, str]:
    gateway = github_gateway(ctx, repo)
    if gateway is None:
        raise HTTPException(503, "GitHub is not connected")
    return gateway, github_connection_used.get()


def _git_proxy_key(ctx: Ctx) -> bytes:
    """Load or create the shared signing key for short-lived worker Git relay capabilities."""
    path = Path(ctx.settings.github_app_dir) / "worker-git-proxy.key"
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        value = b""
    if value and len(value) < 32:
        raise RuntimeError("Git transfer signing key is invalid")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if not value:
        value = secrets.token_bytes(32)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # atomic create: a second API replica reads a complete key
            except FileExistsError:
                value = path.read_bytes()
        finally:
            temporary.unlink(missing_ok=True)
    if len(value) < 32:
        raise RuntimeError("Git transfer signing key is invalid")
    os.chmod(path, 0o600)
    return value


def _issue_git_proxy_token(ctx: Ctx, claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(_git_proxy_key(ctx), payload, hashlib.sha256).digest()
    return payload.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _verify_git_proxy_token(ctx: Ctx, token: str) -> dict[str, Any] | None:
    try:
        encoded, encoded_signature = token.split(".", 1)
        payload = encoded.encode()
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(_git_proxy_key(ctx), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict) or int(claims.get("expires", 0)) < int(utcnow().timestamp()):
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError, OSError):
        return None


def _git_proxy_credential(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        _, separator, token = decoded.partition(":")
        return token if separator else ""
    except (ValueError, UnicodeDecodeError):
        return ""


def _receive_pack_target(body: bytes) -> set[str] | None:
    """Read Git receive-pack command pkt-lines; malformed or empty requests are refused."""
    targets: set[str] = set()
    offset = 0
    while offset < len(body):
        header = body[offset:offset + 4]
        if len(header) != 4:
            return None
        try:
            length = int(header, 16)
        except ValueError:
            return None
        offset += 4
        if length == 0:
            break
        if length < 4 or offset + length - 4 > len(body):
            return None
        packet = body[offset:offset + length - 4]
        offset += length - 4
        fields = packet.split(b"\0", 1)[0].strip().split()
        if len(fields) >= 3 and re.fullmatch(rb"[0-9a-fA-F]{40,64}", fields[0]) and re.fullmatch(
                rb"[0-9a-fA-F]{40,64}", fields[1]):
            if set(fields[1]) == {ord("0")}:
                return None
            targets.add(fields[2].decode("ascii", errors="ignore"))
    return targets or None


def _github_git_upstream(repository: str, suffix: str, method: str, params: list[tuple[str, str]],
                         headers: dict[str, str], body: bytes, token: str) -> httpx.Response:
    """Relay one Git smart HTTP request using a Command Center held access token."""
    with httpx.Client(timeout=180, follow_redirects=False) as client:
        return client.request(method, f"https://github.com/{repository}{suffix}", params=params,
                              headers=headers, content=body, auth=("x-access-token", token))


def validate_github_app_permissions(details: dict[str, Any]) -> None:
    permissions = details.get("permissions") or {}
    required = {"contents": "write", "pull_requests": "write", "workflows": "write", "actions": "read",
                "checks": "read", "statuses": "read", "administration": "write"}
    missing = [name for name, level in required.items() if permissions.get(name) != level]
    if missing:
        raise ValueError("GitHub App does not have the permissions AI Flow requires")


def notify_human_review(ctx: Ctx, key: str, ticket: dict | None = None) -> list[str]:
    """Notify the Jira assignee, or the configured owner, when a required human review starts."""
    ticket = ticket or ctx.db.one("SELECT key, project, labels, summary, fields FROM tickets WHERE key=%s", (key,))
    if not ticket or "require-human" not in (ticket.get("labels") or []):
        return []
    fields = ticket.get("fields") or {}
    assignee = ticket.get("assignee") or fields.get("assignee") or {}
    email = assignee.get("email") if isinstance(assignee, dict) else ""
    title = f"{key} is ready for human review"
    detail = ticket.get("summary") or key
    link = ctx.settings.public_url.rstrip("/") + "/review"
    sent = notify.send("info", title, detail, link, recipient=email or "", ctx=ctx,
                       alert_kind="review_before_merge", project=ticket.get("project", ""), ticket=key,
                       needed="Review the ticket before merge.")
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES ('notify','human-review',%s,'control',true,%s,%s)",
               (key, ",".join(sent) or "no channel configured", J({"assignee": bool(email)})))
    ctx.events.emit("notify.human_review", ticket=key, project=ticket.get("project", ""), account="control",
                    outcome="sent" if sent else "no_channel", reason="require-human")
    return sent


# --- request bodies ---------------------------------------------------------------------------------------
class JoinBody(BaseModel):
    join_token: str
    name: str | None = Field(default=None, min_length=1, max_length=63)
    ip: str = ""


class JoinTokenBody(BaseModel):
    provision_key: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=63)
    role: str = Field(default="worker", min_length=1, max_length=32)
    project: str = Field(min_length=1, max_length=32)


class JoinInspectBody(BaseModel):
    join_token: str = Field(min_length=1, max_length=200)


class HeartbeatBody(BaseModel):
    slots_total: int = 1
    slots_used: int = 0
    release_version: str = ""
    release_protocol: int = 0
    tools: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)  # test services: state per name (docs/v4/test-services.md)
    metrics: dict[str, Any] = Field(default_factory=dict)
    workers: list[dict[str, Any]] = Field(default_factory=list)
    credential_expiries: dict[str, datetime | None] = Field(default_factory=dict)


class ClaimBody(BaseModel):
    ticket: str
    account: str
    step: str = "dev"


class VersionReleaseClaimBody(BaseModel):
    project: str
    version_id: str
    repository: str = ""
    account: str


class VersionReleaseCompleteBody(BaseModel):
    run_id: str
    fence: int
    head_sha: str = ""
    source_sha: str = ""
    checks: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""


class ProjectPreflightBody(BaseModel):
    site: str = Field(min_length=1, max_length=300)
    project: str = Field(min_length=1, max_length=500)
    repo: str = Field(min_length=1, max_length=200)
    base: str = Field(default="develop", min_length=1, max_length=120)
    stack: str = Field(default="", max_length=80)


class ProjectCreateBody(ProjectPreflightBody):
    checks: list[str] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=5, ge=0, le=100)
    parallel_limit: int = Field(default=2, ge=1, le=100)
    merge_method: Literal["merge", "squash", "rebase"] = "squash"


class ProjectRepositoryConfigBody(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    base: str = Field(default="", max_length=120)
    checks: list[str] = Field(default_factory=list, max_length=50)
    stack: str = Field(default="", max_length=80)
    connection: str = Field(default="", max_length=64)


class ProjectRepositoriesBody(BaseModel):
    repositories: list[ProjectRepositoryConfigBody] = Field(default_factory=list, max_length=100)
    epic_repositories: dict[str, str] = Field(default_factory=dict, max_length=500)


class SupportBundleBody(BaseModel):
    ticket_key: str = Field(default="", max_length=40)


class GitHubConnectionTokenBody(BaseModel):
    token: str = Field(min_length=20, max_length=600, repr=False)
    label: str = Field(default="", max_length=80)
    account: str = Field(default="", max_length=39)


class GitHubInstallationBody(BaseModel):
    installation_id: int = Field(ge=1)


class ProjectPauseBody(BaseModel):
    paused: bool = True


class CheckProbeBody(BaseModel):
    project: str
    signature: str
    results: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class CheckProbeTokenBody(BaseModel):
    project: str
    repo: str


class FenceBody(BaseModel):
    fence: int
    progress: dict[str, Any] = Field(default_factory=dict)


class ResultBody(BaseModel):
    fence: int
    outcome: str
    error_kind: str = ""
    reset_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    branch: str = Field(default="", max_length=240)
    pr_url: str = ""
    head_sha: str = ""
    summary: str = ""
    quota: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class JiraWrite(BaseModel):
    run_id: str
    fence: int
    to: str = ""
    body: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class GitHubTokenBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    branch: str = ""


class PRBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    head: str
    base: str
    title: str
    body: str = ""
    draft: bool = True


class RoleHealthSettingsBody(BaseModel):
    window_runs: int
    failure_percent: int


class ProbeBody(BaseModel):
    ok: bool
    error_kind: str = ""
    reset_at: datetime | None = None
    quota: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""
    credential_expires_at: datetime | None = None


class MergeBody(BaseModel):
    run_id: str
    fence: int
    number: int
    title: str = ""
    method: str = "squash"


class PRCommentBody(BaseModel):
    run_id: str
    fence: int
    number: int
    body: str


class BranchCleanupBody(BaseModel):
    run_id: str
    fence: int
    branch: str


class TriageAlertBody(BaseModel):
    run_id: str
    fence: int
    detail: str


class EscalationRaiseBody(BaseModel):
    run_id: str
    fence: int
    question: str = Field(min_length=1, max_length=6000)


class TriageDecisionBody(BaseModel):
    run_id: str
    fence: int
    decision: str
    question: str = ""
    answer: str = ""
    why: str = ""
    escalation_class: str = ""
    options: list[str] = Field(default_factory=list)
    recommended: str = ""
    topic: str = ""
    keywords: list[str] = Field(default_factory=list)
    criterion: str = ""
    narrowed: str = ""


class OwnerDecisionBody(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    decision: str = Field(min_length=3, max_length=3000)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    patterns: list[str] = Field(default_factory=list, max_length=8)
    project: str = Field(default="", max_length=40)


class OwnerDecisionAnswerBody(BaseModel):
    choice: str = Field(default="recommended", max_length=1000)
    note: str = Field(default="", max_length=1000)


class ReviewerBody(BaseModel):
    comment: str = ""
    reason: str = ""
    question: str = ""
    answer: str = ""


class MasterActionBody(BaseModel):
    run_id: str
    fence: int
    action: str
    ticket: str = ""
    intent: str = ""
    category: str = ""  # infrastructure | secret | production_release: always escalated to the owner
    headline: str = "follow up"
    what: str = ""
    why: str = ""
    result: str = ""
    next: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    kind: str = "master_follow_up"
    subject: str = ""
    severity: str = "warning"
    title: str = ""
    detail: str = ""
    target_status: str = ""
    options: list[str] = Field(default_factory=list)
    observation_key: str = ""
    account_id: str = ""
    pause_minutes: int = 30
    target_run_id: str = ""
    signature: str = ""


class MasterFlowBugsBody(BaseModel):
    run_id: str
    fence: int


class MasterPlanOutcomeBody(BaseModel):
    run_id: str
    fence: int
    valid: bool


class MasterBudgetBody(BaseModel):
    daily_budget_minutes: int = Field(ge=0, le=1440)


class ChangeRequestBody(BaseModel):
    title: str = ""
    description: str = ""
    epic: str = ""


class CloseProjectBody(BaseModel):
    actor: str = "owner"


class KnowledgeBody(BaseModel):
    id: str = ""
    type: str
    title: str
    content: str
    actor: str
    archived: bool = False
    version: int | None = None


class KnowledgeApprovalBody(BaseModel):
    approved_by: str = "owner"


class KnowledgeRestoreBody(BaseModel):
    version: int
    actor: str


class ConsoleUserCreateBody(BaseModel):
    email: str
    display_name: str = ""
    roles: list[str]
    project_keys: list[str] | None = None


class ConsoleUserUpdateBody(BaseModel):
    roles: list[str]
    project_keys: list[str] | None = None


class ConsoleUserDisabledBody(BaseModel):
    disabled: bool


class ViewAsBody(BaseModel):
    role: str


class BackupConfigBody(BaseModel):
    schedule: Literal["daily", "weekly"] = "daily"
    destination_kind: Literal["local", "mounted", "s3"] = "local"
    destination_path: str = "/srv/aiflow/backups"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_region: str = "us-east-1"
    retention_days: int = Field(default=30, ge=1, le=3650)


class KnowledgeItemBody(BaseModel):
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="project", max_length=16)
    epic: str = Field(default="", max_length=100)
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=30_000)
    actor: str = Field(default="", max_length=200)


class KnowledgeItemActionBody(BaseModel):
    actor: str = Field(default="", max_length=200)
    project: str = Field(default="", max_length=32)
    scope: str = Field(default="", max_length=16)
    epic: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=30_000)
    duplicate_id: int | None = None


class StakeholderVerdictBody(BaseModel):
    change_key: str
    target_key: str = ""
    environment: Literal["develop", "production"]
    verdict: Literal["expected", "not_expected"]
    sender: str = Field(min_length=1, max_length=200)
    release_commit: str = Field(default="", max_length=500)
    expected: str = Field(default="", max_length=10_000)
    actual: str = Field(default="", max_length=10_000)
    screenshot_data: str = Field(default="", max_length=7_000_000)
    screenshot_filename: str = Field(default="screenshot.png", max_length=255)


class EventsBody(BaseModel):
    events: list[dict[str, Any]]


class WorktreeSweepBody(BaseModel):
    dirs: list[str] = []   # "<project>/<directory>" entries found under /srv/aiflow/work on the node


class ReleaseFailureBody(BaseModel):
    release_version: str
    detail: str = ""


class NodeOperationBody(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["install", "repair"]
    component: str = ""


class NodeOperationResultBody(BaseModel):
    status: Literal["completed", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)


class JoinCommandBody(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=63)
    role: str = Field(default="", max_length=32)
    project: str = Field(default="", max_length=32)


class LicenseKeyBody(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(min_length=1, max_length=2000)


class OfflineLicenseBody(BaseModel):
    model_config = {"extra": "forbid"}
    license_file: str = Field(min_length=1, max_length=50000)


class PlanLimitsBody(BaseModel):
    model_config = {"extra": "forbid"}
    plan: str
    limits: dict[str, int]


class ManagedAccountCreateBody(BaseModel):
    model_config = {"extra": "forbid"}
    provider: Literal["claude", "codex", "opencode", "grok"]
    sessions: int = Field(ge=1, le=8)
    jobs: list[Literal["develop", "verify", "intake", "master"]] = Field(min_length=1, max_length=4)


class ManagedAccountUpdateBody(BaseModel):
    model_config = {"extra": "forbid"}
    sessions: int | None = Field(default=None, ge=1, le=8)
    jobs: list[Literal["develop", "verify", "intake", "master"]] | None = Field(default=None, min_length=1,
                                                                                  max_length=4)
    action: Literal["pause", "resume"] | None = None


class ConfirmBody(BaseModel):
    model_config = {"extra": "forbid"}
    confirm: bool = False


class ManagedAccountSignInStatusBody(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: str = Field(min_length=1, max_length=64)
    status: Literal["starting", "waiting", "completed", "failed"]
    sign_in_url: str = Field(default="", max_length=2048)
    code: str = Field(default="", max_length=80)
    error: str = Field(default="", max_length=300)


# --- app --------------------------------------------------------------------------------------------------
def create_app(ctx: Ctx) -> FastAPI:
    from contextlib import asynccontextmanager

    configured_repo = getattr(ctx, "_configured_repo", ctx.repo)
    ctx._configured_repo = configured_repo

    def runtime_repo() -> ControlRepo:
        """Merge console-managed accounts over the control-repo snapshot for live worker scheduling."""
        base = configured_repo()
        removed_nodes = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE removed_at IS NOT NULL")}
        removed_ids = {row["id"] for row in ctx.db.all(
            "SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}
        managed = [row for row in ctx.db.all(
            "SELECT * FROM managed_ai_accounts WHERE removed_at IS NULL ORDER BY created_at,id")
                   if row["node"] not in removed_nodes]
        if not managed and not removed_nodes and not removed_ids:
            return base
        managed_ids = {row["id"] for row in managed}
        accounts = {account.id: account for account in base.accounts
                    if account.id not in managed_ids and account.id not in removed_ids
                    and account.node not in removed_nodes}
        for row in managed:
            accounts[row["id"]] = Account(
                id=row["id"], provider=row["provider"], node=row["node"], tier=row["tier"],
                roles=list(row["roles"] or []), parallel=int(row["parallel"]),
                explicit_only=bool(row["explicit_only"]), paused=bool(row["paused"]))
        return replace(base, accounts=list(accounts.values()))

    # The registry is the live source for screen-managed accounts. Configured accounts stay supported,
    # while the console can add and edit account assignments without a control-repo file change.
    ctx.repo = runtime_repo

    @asynccontextmanager
    async def lifespan(_app):
        try:
            licensing.initialize(ctx.db)
        except Exception as e:  # noqa: BLE001
            log.warning("licence state initialization failed (%s)", type(e).__name__)
        try:
            _app.state.sync_accounts()
        except Exception as e:  # noqa: BLE001
            log.warning("account sync failed: %s", e)
        yield

    app = FastAPI(title="AI Flow v4 control API", version=VERSION, lifespan=lifespan)
    app.state.ctx = ctx
    support_bundle.control_log_buffer(*(v if isinstance(v, str) else "" for v in (ctx.settings.support_log_dir, ctx.settings.o2_source)))
    app.state.license_sync_warnings = []
    # Release reconciliation loops receive the same per-repository routing as API handlers.
    ctx.github_connection_for_repository = lambda repository: github_context_for_repository(ctx, repository)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Return safe 422 details without echoing rejected credentials or licence material."""
        if request.url.path in {"/v1/licensing/activate", "/v1/licensing/offline"}:
            return JSONResponse(
                {"detail": "Licence key or signed licence file is invalid or exceeds the supported size."},
                status_code=422,
            )
        errors = []
        for error in exc.errors():
            errors.append({"loc": list(error.get("loc") or ()), "msg": "Invalid value",
                           "type": error.get("type", "value_error")})
        return JSONResponse(status_code=422, content={"detail": errors})

    def sync_accounts() -> list[str]:
        """Sync configured accounts and machines that fit the current licence limits."""
        repo = configured_repo()
        warnings: list[str] = []
        removed = {row["id"] for row in ctx.db.all(
            "SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}
        removed_nodes = {row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE removed_at IS NOT NULL")}
        for a in repo.accounts:
            if a.id in removed or a.node in removed_nodes:
                continue
            if not ctx.db.one("SELECT 1 FROM accounts WHERE id=%s", (a.id,)):
                try:
                    # Startup admits configured accounts in order; use registered rows for admission because the
                    # Licence screen's usage count also includes configured accounts that have not synced yet.
                    registered_count = int(ctx.db.one("SELECT count(*) AS count FROM accounts")["count"])
                    licensing.require_capacity(ctx, "ai_accounts", count=registered_count,
                                                public_key=ctx.settings.license_public_key)
                except licensing.LicenseError as exc:
                    message = f"Configured AI account {a.id} was not synced: {exc}"
                    warnings.append(message)
                    log.warning("%s", message)
                    continue
            ctx.db.run(
                "INSERT INTO accounts (id, provider, node, tier, roles) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider, node=EXCLUDED.node, tier=EXCLUDED.tier,"
                " roles=EXCLUDED.roles, updated_at=now()", (a.id, a.provider, a.node, a.tier, a.roles))
        for n in repo.nodes.values():
            removed_node = ctx.db.one("SELECT removed_at FROM nodes WHERE name=%s", (n.name,))
            if removed_node and removed_node["removed_at"]:
                continue
            if not removed_node:
                try:
                    licensing.require_capacity(ctx, "machines", public_key=ctx.settings.license_public_key)
                except licensing.LicenseError as exc:
                    message = f"Configured machine {n.name} was not synced: {exc}"
                    warnings.append(message)
                    log.warning("%s", message)
                    continue
            # Keep the IP a node reported at join: a restart must not replace where it was last seen with
            # the control repo value. Only a first insert (no join yet) takes the configured address.
            ctx.db.run("INSERT INTO nodes (name, ip, role, slots_total, projects) VALUES (%s,%s,%s,%s,%s)"
                       " ON CONFLICT (name) DO UPDATE SET role=EXCLUDED.role, slots_total=EXCLUDED.slots_total,"
                       " projects=EXCLUDED.projects, dynamic=false",
                       (n.name, n.ip, n.role, n.slots, n.projects))
        app.state.license_sync_warnings = warnings
        return warnings

    app.state.sync_accounts = sync_accounts
    _identity: list[tuple[str, str, str]] = []

    def bot_identity() -> tuple[str, str]:
        github = github_gateway(ctx)
        if not github:
            return "aiflow4", "aiflow4@localhost"
        app_id = str((getattr(github, "meta", {}) or {}).get("id") or "")
        if not _identity or _identity[0][0] != app_id:
            name, email = github.bot_identity()
            _identity[:] = [(app_id, name, email)]
        return _identity[0][1], _identity[0][2]

    # --- auth -----------------------------------------------------------------------------------------------
    def node_auth(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        row = ctx.db.one("SELECT name FROM nodes WHERE token_hash = %s AND token_hash <> '' AND removed_at IS NULL",
                         (h(token),)) if token else None
        if not row:
            raise HTTPException(401, "unknown node token")
        return row["name"]

    def release_reader(authorization: str = Header(default="")) -> str:
        """A node token, or an unexpired, unconsumed join token so a new machine can fetch the signed release."""
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            row = ctx.db.one("SELECT node_name FROM node_join_tokens WHERE token_hash=%s AND used_at IS NULL"
                             " AND expires_at > now()", (h(token),))
            if row:
                return row["node_name"]
        return node_auth(authorization)

    def reviewer_auth(request: Request, authorization: str = Header(default="")) -> str:
        """The legacy reviewer bearer token, or a console session whose roles grant `review.actions`."""
        configured = ctx.settings.reviewer_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            return "reviewer"
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user:
            ticket_key = str(request.path_params.get("key", ""))
            row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket_key,)) if ticket_key else None
            in_scope = (user.project_keys is None or not row or row["project"] in user.project_keys)
            if has_permission(user.effective_roles, "review.actions") and in_scope:
                return user.subject
            audit("console", "access_denied", ticket_key, "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "reviewer actions are not configured")
        raise HTTPException(401, "reviewer authorization required", headers={"WWW-Authenticate": "Bearer"})

    def owner_auth(request: Request, authorization: str = Header(default="")) -> str:
        def writable() -> None:
            try:
                licensing.require_writable(ctx.db, ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None

        configured = ctx.settings.owner_token
        scheme, separator, token = authorization.partition(" ")
        if (configured and separator and scheme.lower() == "bearer" and token.strip()
                and secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            writable()
            return ""
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if user and has_permission(user.effective_roles, "knowledge.approve"):
            writable()
            return user.subject
        if not configured and not ctx.settings.console_session_secret:
            raise HTTPException(503, "owner actions are not configured")
        if user:
            audit("aiflow-control", "knowledge_approve", "", "", None, user.subject, False,
                  "not available for your role")
            raise HTTPException(403, "not available for your role")
        raise HTTPException(401, "owner authorization required", headers={"WWW-Authenticate": "Bearer"})

    def deploy_auth(authorization: str = Header(default="")) -> str:
        """The deploy scripts on the control machine sign in with the owner token (a bearer token, never a cookie)."""
        configured = ctx.settings.owner_token
        if not configured:
            raise HTTPException(503, "deploy access is not configured")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or not secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            raise HTTPException(401, "deploy authorization required", headers={"WWW-Authenticate": "Bearer"})
        return "deploy"

    def audit(target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
              result: str, payload: dict | None = None) -> None:
        with ctx.db.pool.connection() as connection:
            audit_in(connection, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def audit_in(connection, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str,
                  accepted: bool, result: str, payload: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (target, action, ticket, run_id, fence, actor, accepted, result[:500],
             J(with_connection(target, payload))))

    def with_connection(target: str, payload: dict | None) -> dict:
        """Name the GitHub connection behind a GitHub write; it is an id, never a credential."""
        payload = dict(payload or {})
        used = github_connection_used.get()
        if used and target in ("github", "aiflow-control") and "connection" not in payload:
            payload["connection"] = used
        return payload

    def console_user(request: Request, permissions: set[str] | str | None, target: str = "console", action: str = "read",
                     node: str = "") -> ConsoleUser:
        """Require a signed, unexpired console session. Role and subject headers are deliberately ignored."""
        secret = ctx.settings.console_session_secret
        if len(secret.encode()) < 32:
            if action != "read":
                audit(target, action, node, "", None, "", False, "console session signing key is not configured")
            raise HTTPException(503, "console session authentication is not configured")
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user:
            if action != "read":
                audit(target, action, node, "", None, "", False, "authenticated console session required")
            raise HTTPException(401, "authenticated console session required")
        required = {permissions} if isinstance(permissions, str) else permissions
        if required is not None and not role_satisfies(user.effective_roles, required):
            if action != "read":
                audit(target, action, node, "", None, user.subject, False, "not available for your role")
            raise HTTPException(403, "not available for your role")
        read_actions = {"read", "project_catalog", "project_repositories", "project_preflight",
                        "list_import_installations", "open_installation_settings"}
        if action not in read_actions | {"license_activate"}:
            try:
                licensing.require_writable(ctx.db, ctx.settings.license_public_key)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None
        return user

    def optional_console_user(request: Request) -> ConsoleUser | None:
        """Read a session when present without changing the legacy open-console mode."""
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        return resolve_session_user(ctx.db, claims) if claims else None

    def require_capacity(resource: str, count: int | None = None, *, connection=None) -> dict:
        try:
            return licensing.require_capacity(ctx, resource, count, public_key=ctx.settings.license_public_key,
                                              connection=connection)
        except licensing.LicenseError as exc:
            raise HTTPException(409, str(exc)) from None

    def run_row(run_id: str) -> dict:
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(404, "unknown run")
        row = ctx.db.one("SELECT * FROM runs WHERE id = %s", (run_id,))
        if not row:
            raise HTTPException(404, "unknown run")
        return row

    def fenced(run_id: str, fence: int, target: str, action: str, payload: dict | None = None) -> dict:
        run = run_row(run_id)
        ok = int(run["fence"]) == int(fence) and ctx.leases.valid(run["ticket_key"], str(run["id"]), int(fence))
        if not ok:
            audit(target, action, run["ticket_key"], run_id, fence, run["account_id"], False, "stale fencing token", payload)
            ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                            account=run["account_id"], node=run["node"], reason=f"{target}.{action}: stale fence")
            raise HTTPException(409, "fencing token is no longer valid")
        if run["step"] == "master" and target != "master":
            audit(target, action, run["ticket_key"], run_id, fence, run["account_id"], False,
                  "master writes must use the restricted master action API", payload)
            ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                            account=run["account_id"], node=run["node"],
                            reason=f"{target}.{action}: master writes require the restricted action API")
            raise HTTPException(403, "master writes must use the restricted master action API")
        return run

    def master_run(run_id: str, fence: int, node: str, action: str, payload: dict | None = None) -> dict:
        run = fenced(run_id, fence, "master", action, payload)
        if run["step"] != "master":
            raise HTTPException(403, "only a master run can use this endpoint")
        if run["node"] != node:
            raise HTTPException(403, "master run belongs to another node")
        return run

    def reviewer_run(key: str, statuses: tuple[str, ...] = ("Human review",),
                     allowed_kinds: tuple[str, ...] | None = None) -> tuple[dict, str, int, int]:
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        ticket = ctx.db.one("SELECT key, project, status, labels, summary, blocked_by FROM tickets WHERE key=%s", (key,))
        if not ticket:
            raise HTTPException(404, "unknown ticket")
        if ticket["status"] not in statuses:
            raise HTTPException(409, "ticket is no longer waiting for this review action")
        if allowed_kinds is not None and current_review_item_kind(ticket) not in allowed_kinds:
            raise HTTPException(409, "this action does not apply to the current human review item")
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        pr_run = ctx.db.one("SELECT pr_url,details FROM runs WHERE ticket_key=%s AND pr_url<>''"
                            " ORDER BY started_at DESC LIMIT 1", (key,))
        match = re.search(r"/pull/(\d+)(?:/|$)", (pr_run or {}).get("pr_url", ""))
        number = int(match.group(1)) if match else 0
        if number:
            repository = str(((pr_run or {}).get("details") or {}).get("repository") or "")
            project = ctx.repo().projects.get(ticket["project"])
            repository = repository or (repository_for_run(pr_run or {}, project) if project else "")
            if not project or not repository:
                raise HTTPException(404, "project repository is not configured")
            github_gateway(ctx, repository)
        run_id = str(uuid.uuid4())
        claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
        if not claim.ok:
            raise HTTPException(409, "ticket is already being changed")
        try:
            run_details = {"repository": str(((pr_run or {}).get("details") or {}).get("repository") or "")}
            ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash,details)"
                        " VALUES (%s,%s,%s,'qa','reviewer-console','human','control',%s,'',%s)",
                        (run_id, key, ticket["project"], claim.fence, J(run_details)))
        except Exception:
            ctx.leases.release(key, run_id, claim.fence)
            raise
        return ticket, run_id, claim.fence, number

    def finish_reviewer_run(key: str, run_id: str, fence: int, outcome: str, summary: str) -> None:
        ctx.db.run("UPDATE runs SET status='finished', outcome=%s, summary=%s, ended_at=now() WHERE id=%s",
                   (outcome, summary[:2000], run_id))
        try:
            knowledge.capture_run(ctx.db, run_id, ctx.settings.jira_site)
            knowledge.capture_closed_tickets(ctx.db, ctx.settings.jira_site)
        except Exception as exc:  # noqa: BLE001 - learning must not block a review action
            log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
        ctx.leases.release(key, run_id, fence)
        ctx.events.emit("run.finished", ticket=key, project=key.split("-", 1)[0], step="qa", run_id=run_id,
                        fence=fence, account="reviewer-console", provider="human", node="control", outcome=outcome,
                        reason=summary[:200])

    def reviewer_comment(ticket: str, run_id: str, fence: int, number: int, text: str,
                         source: str = "human-review") -> None:
        """Mirror a reviewer action to Jira and, when the ticket has a PR, its PR conversation."""
        jira_run = fenced(run_id, fence, "jira", "comment", {"chars": len(text)})
        comment_id = ctx.jira.comment(ticket, text)
        audit("jira", "comment", ticket, run_id, fence, jira_run["account_id"], True, comment_id,
              {"source": source, "chars": len(text)})
        if number:
            github_run = fenced(run_id, fence, "github", "comment", {"number": number})
            project = ctx.repo().projects.get(github_run["project"])
            if not project:
                raise HTTPException(404, "project is not configured")
            repository = repository_for_run(github_run, project)
            gateway = github_gateway(ctx, repository)
            gateway.comment(repository.split("/", 1)[-1], number, text)
            audit("github", "comment", ticket, run_id, fence, github_run["account_id"], True, "ok",
                  {"source": source, "number": number, "repo": repository})

    def reviewer_transition(ticket: str, run_id: str, fence: int, target: str, reason: str) -> str:
        transition_run = fenced(run_id, fence, "jira", "transition", {"to": target})
        status = ctx.jira.transition_to(ticket, target)
        ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN %s='Done' THEN coalesce(done_at,now())"
                   " ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, ticket))
        audit("jira", "transition", ticket, run_id, fence, transition_run["account_id"], True, status,
              {"to": target, "source": "human-review"})
        ctx.events.emit("jira.transition", ticket=ticket, project=transition_run["project"], run_id=run_id,
                        fence=fence, account="reviewer-console", node="control", outcome=status, reason=reason)
        return status

    def reviewer_labels(ticket: str, run_id: str, fence: int, add: tuple[str, ...] = (),
                        remove: tuple[str, ...] = ()) -> None:
        if not add and not remove:
            return
        fields_run = fenced(run_id, fence, "jira", "fields", {"add": list(add), "remove": list(remove)})
        for label in add:
            ctx.jira.add_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s=ANY(labels) THEN labels"
                       " ELSE array_append(labels,%s) END WHERE key=%s", (label, label, ticket))
        for label in remove:
            ctx.jira.remove_label(ticket, label)
            ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket))
        audit("jira", "fields", ticket, run_id, fence, fields_run["account_id"], True, "review labels updated",
              {"source": "human-review", "add": list(add), "remove": list(remove)})

    def record_reviewer_action(ticket: dict, run_id: str, fence: int, action: str, text: str = "") -> None:
        latest_dev = ctx.db.one("SELECT step,outcome FROM runs WHERE ticket_key=%s AND step='dev'"
                                " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
        kind = review_item_kind(ticket, latest_dev)
        audit("human-review", action, ticket["key"], run_id, fence, "reviewer-console", True, "accepted",
              {"kind": kind, "text": text[:4000]})
        ctx.events.emit("review.action", ticket=ticket["key"], project=ticket["project"], step="qa",
                        run_id=run_id, fence=fence, account="reviewer-console", node="control", outcome=action,
                        action=action, kind=kind, text=text[:4000])

    def review_item_kind(ticket: dict, latest_run: dict | None = None) -> str:
        if ticket["status"] == "Human review":
            return "review_before_merge"
        if ticket["status"] == "Needs owner":
            if ticket.get("_has_open_blocker", bool(ticket.get("blocked_by"))):
                return "blocked"
            if "owner-escalated" in (ticket.get("labels") or []):
                return "decision_needed"
            asked = ctx.db.one("SELECT ts FROM events WHERE ticket=%s AND type='review.action'"
                               " AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1", (ticket["key"],))
            triaged = ctx.db.one("SELECT started_at,status,outcome,ended_at FROM runs WHERE ticket_key=%s AND step='triage'"
                                 " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
            if asked and (not triaged or triaged["status"] == "running"
                          or triaged["outcome"] in ("failed", "timeout", "lost")
                          or (triaged["ended_at"] and asked["ts"] > triaged["ended_at"])):
                return "question_from_ai"
            if latest_run and latest_run.get("step") == "dev" and latest_run.get("outcome") == "blocked":
                return "question_from_ai"
            return "decision_needed"
        return "blocked"

    def current_review_item_kind(ticket: dict) -> str:
        """Classify a waiting ticket with the same live blocker check used by the review queue."""
        blockers = [str(blocker) for blocker in (ticket.get("blocked_by") or [])]
        statuses = {row["key"]: row["status"] for row in ctx.db.all(
            "SELECT key,status FROM tickets WHERE key=ANY(%s)", (blockers,))} if blockers else {}
        has_open_blocker = any(statuses.get(blocker) not in ("Done", "Closed") for blocker in blockers)
        latest = ctx.db.one("SELECT step,outcome FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1",
                            (ticket["key"],))
        return review_item_kind({**ticket, "_has_open_blocker": has_open_blocker}, latest)

    def review_thread(key: str) -> list[dict]:
        thread = []
        actions = ctx.db.all("SELECT ts, data FROM events WHERE ticket=%s AND type='review.action'"
                             " ORDER BY ts DESC LIMIT 40", (key,))
        for row in reversed(actions):
            data = row.get("data") or {}
            action = str(data.get("action") or "")
            text = str(data.get("text") or "")
            if action in {"ask-ai", "answer"} and text:
                thread.append({"kind": "question" if action == "ask-ai" else "answer",
                               "author": "Reviewer", "text": text, "created_at": row["ts"].isoformat()})
        developer_questions = ctx.db.all("SELECT summary,started_at FROM runs WHERE ticket_key=%s AND step='dev'"
                                         " AND outcome='blocked' ORDER BY started_at DESC LIMIT 20", (key,))
        for row in developer_questions:
            if row.get("summary"):
                thread.append({"kind": "question", "author": "AI", "text": row["summary"],
                               "created_at": row["started_at"].isoformat()})
        answers = ctx.db.all("SELECT summary, details, outcome, started_at FROM runs WHERE ticket_key=%s"
                             " AND step='triage' AND outcome IN ('answered','escalated')"
                             " ORDER BY started_at DESC LIMIT 20", (key,))
        for row in answers:
            details = row.get("details") or {}
            answer = str(details.get("answer") or row.get("summary") or "")
            if row.get("outcome") == "escalated":
                answer = f"Could not answer safely: {details.get('why') or answer}"
            if answer:
                thread.append({"kind": "ai_answer", "author": "AI", "text": answer,
                               "created_at": row["started_at"].isoformat()})
        return sorted(thread, key=lambda item: item["created_at"])

    def repository_for_run(run: dict[str, Any], project=None) -> str:
        """Use the repository captured when the run was claimed, with a legacy settings fallback."""
        saved = str(((run.get("details") or {}).get("repository") or ""))
        if saved:
            return saved
        project = project or ctx.repo().projects.get(run.get("project"))
        if not project:
            return ""
        ticket = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (run.get("ticket_key"),)) or {}
        ticket["epic"] = ticket_epic(ticket)
        return project.repo_for(ticket)

    def settings_for_run(run: dict[str, Any], project) -> dict[str, Any]:
        settings = (run.get("details") or {}).get("repository_settings")
        if isinstance(settings, dict) and settings.get("repo"):
            return settings
        return project.settings_for_repo(repository_for_run(run, project))

    def matches_run_repository(repository: str, requested: str) -> bool:
        requested = requested.strip().lower()
        return requested in {repository.lower(), repository.split("/", 1)[-1].lower()}

    def merge_command(run: dict, number: int, title: str = "", method: str = "squash", node: str = "") -> dict:
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        repo_name = repository.split("/", 1)[-1]
        res = github.merge_pr(repo_name, number, title, method)
        ok = bool(res.get("merged"))
        audit("github", "merge", run["ticket_key"], str(run["id"]), run["fence"], run["account_id"], ok,
              res.get("sha") or res.get("message", ""), {"number": number, "method": method,
                                                             "repo": repository})
        ctx.events.emit("github.merge", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                        account=run["account_id"], node=node or run["node"],
                        outcome="merged" if ok else "refused", reason=res.get("message", "")[:200])
        return res

    def master_budget_state() -> dict[str, Any]:
        setting = ctx.db.one("SELECT daily_budget_minutes FROM master_settings WHERE singleton=true") or {
            "daily_budget_minutes": DEFAULT_MASTER_DAILY_BUDGET_MINUTES}
        budget = int(setting["daily_budget_minutes"])
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        usage = ctx.db.one(
            "SELECT coalesce(sum(coalesce(nullif(r.details->>'model_time_s','')::numeric,"
            " extract(epoch FROM (coalesce(r.ended_at,now())-r.started_at))) / 60),0) AS used_minutes"
            " FROM runs r WHERE r.step='master' AND r.started_at >= %s", (day_start,)) or {}
        used = max(0.0, float(usage.get("used_minutes") or 0))
        remaining = max(0, int(budget * 60 - used * 60))
        return {"daily_budget_minutes": budget, "used_minutes": round(used, 1),
                "remaining_minutes": round(max(0, budget - used), 1), "remaining_seconds": remaining,
                "enabled": budget > 0 and remaining > 0}

    def account_master_paused(account_id: str) -> bool:
        return bool(ctx.db.one("SELECT 1 AS paused FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account_id,)))

    def quota_forecast_at_risk(account_id: str) -> bool:
        """Read the latest watchdog forecast without rebuilding weeks of run and probe history per poll."""
        row = ctx.db.one("SELECT quota_at_risk FROM accounts WHERE id=%s", (account_id,))
        return bool(row and row["quota_at_risk"])

    def quota_peer_accounts(account_id: str, roles: set[str] | None = None) -> list[dict]:
        """Ready same-provider accounts that can serve at least one of this account's workflow roles."""
        account_row = ctx.db.one("SELECT provider FROM accounts WHERE id=%s", (account_id,))
        if not account_row:
            return []
        peers = ctx.db.all(
            "SELECT a.id,a.node,a.roles,a.status,a.quota_at_risk FROM accounts a"
            " JOIN nodes n ON n.name=a.node WHERE a.provider=%s AND a.id<>%s AND n.status='online'"
            " AND a.status NOT IN ('cooldown','signed_out','error') ORDER BY a.id",
            (account_row["provider"], account_id))
        available: list[dict] = []
        for peer in peers:
            peer_roles = set(peer.get("roles") or ["dev", "verify"])
            if roles and not roles.intersection(peer_roles):
                continue
            if roles == {"master"} and peer["status"] not in ("idle", "working", "probing"):
                continue
            if peer["quota_at_risk"] or ctx.leases.cooling(peer["id"]) or account_master_paused(peer["id"]):
                continue
            available.append(peer)
        return available

    def account_quota_at_risk(account_id: str, roles: set[str] | None = None) -> bool:
        """Defer a risky account only while another ready account of the same provider can take its role."""
        if not quota_forecast_at_risk(account_id):
            return False
        return bool(quota_peer_accounts(account_id, roles))

    def quota_peer_release_candidates(account_id: str) -> set[tuple[str, str, str]]:
        """Release work a ready peer can actually claim for this quota-risky account."""
        if not quota_forecast_at_risk(account_id):
            return set()
        available: set[tuple[str, str, str]] = set()
        for peer in quota_peer_accounts(account_id, {"master", "release"}):
            if not rollout.node_compatible(ctx, peer["node"], RELEASES)["compatible"]:
                continue
            for candidate in version_release.candidates(ctx, peer["id"], peer["node"]):
                available.add((candidate["project"], str(candidate["version_id"]),
                               str(candidate.get("repository") or "")))
        return available

    def master_snapshot() -> dict:
        now = utcnow()
        repo = ctx.repo()
        incidents = ctx.db.all("SELECT id, opened_at, severity, kind, subject, title, detail FROM incidents"
                               " WHERE closed_at IS NULL ORDER BY opened_at")
        needs_owner = ctx.db.all("SELECT key, project, summary, description, priority, labels, jira_updated,"
                                 " ('owner-escalated'=ANY(labels)) AS owner_escalated FROM tickets"
                                 " WHERE status='Needs owner'"
                                 " ORDER BY priority, jira_created NULLS LAST, key")
        running = ctx.db.all("SELECT id, ticket_key, project, step, account_id, provider, model, node, fence, started_at"
                             " FROM runs WHERE status='running' AND step<>'master' ORDER BY started_at")
        clocks = []
        for run in running:
            project = repo.projects.get(run["project"])
            limits = project.work_minutes() if project else {"target": 30, "warn": 45, "hard": 60}
            elapsed = max(0, (now - run["started_at"]).total_seconds() / 60)
            clocks.append({"run_id": str(run["id"]), "ticket": run["ticket_key"], "project": run["project"],
                           "step": run["step"], "account": run["account_id"], "provider": run["provider"],
                           "model": run["model"], "node": run["node"], "fence": run["fence"],
                           "started_at": run["started_at"].isoformat(), "elapsed_minutes": round(elapsed, 1),
                           "work_minutes": limits,
                           "passed": [name for name in ("target", "warn", "hard") if elapsed >= limits[name]]})
        offline_nodes = ctx.db.all("SELECT name, last_heartbeat FROM nodes WHERE status='offline' ORDER BY name")
        quota_incidents = [i for i in incidents if i["kind"] in ("account_quota", "quota_high")]
        high_subjects = {i["subject"].split(":", 1)[0] for i in quota_incidents if i["kind"] == "quota_high"}
        quota_accounts = []
        for account in ctx.db.all("SELECT id, provider, node, status, cooldown_until, last_error, quota FROM accounts"):
            cooling = ctx.leases.cooling(account["id"])
            pause = ctx.db.one("SELECT paused_until, reason FROM master_account_pauses"
                               " WHERE account_id=%s AND paused_until > now()", (account["id"],))
            if (account["status"] in ("cooldown", "signed_out", "error") or cooling
                    or account["id"] in high_subjects or pause):
                quota_accounts.append({**account, "cooldown": {"reason": cooling[0], "seconds": cooling[1]}
                                      if cooling else None, "master_pause": pause})
        return {"scope": "global", "captured_at": now.isoformat(), "open_incidents": incidents,
                "needs_owner": needs_owner, "running_clocks": clocks, "offline_nodes": offline_nodes,
                "quota_accounts": quota_accounts, "quota_incidents": quota_incidents,
                "autonomy": master_budget_state()}

    def process_flow_bugs(run: dict) -> list[dict]:
        """Create or update detected TESTFLOW Bugs from this valid, fenced master cycle."""
        if not ctx.jira:
            return []
        reports: list[dict] = []
        terminal = {"done", "closed", "resolved", "cancelled", "canceled"}
        for candidate in flow_bug_candidates(ctx):
            tracker = ctx.db.one("SELECT ticket_key, run_ids, repeat_count, in_intake FROM flow_bug_tracker"
                                 " WHERE signature=%s", (candidate["signature"],))
            if tracker:
                ticket_key = tracker["ticket_key"]
                try:
                    status = ctx.jira.status(ticket_key)
                except Exception as exc:  # Jira reads can fail transiently; retry on the next master cycle.
                    log.warning("could not check status of flow-bug %s: %s", ticket_key, type(exc).__name__)
                    continue
                ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket_key))
                if status.casefold() not in terminal:
                    if not tracker["in_intake"]:  # created earlier, but the move to Intake failed: retry only that
                        if status == "Intake":
                            ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now()"
                                       " WHERE signature=%s", (candidate["signature"],))
                        elif move_flow_bug_to_intake(run, ticket_key, candidate["signature"]):
                            reports.append({"action": "moved_to_intake", "ticket": ticket_key,
                                            "signature": candidate["signature"]})
                    known = set(tracker["run_ids"] or [])
                    new_runs = [r for r in candidate["runs"] if r["id"] not in known]
                    if new_runs:
                        repeat_count = int(tracker["repeat_count"]) + len(new_runs)
                        total = len(known) + len(new_runs)
                        body = ("AI Flow · master · repeated flow error\n"
                                f"Pattern: {candidate['pattern']}\n"
                                f"Repeat count: {repeat_count} additional failed runs since this Bug was opened "
                                f"({total} matching runs recorded).\n"
                                f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                                "New run IDs: " + ", ".join(r["id"] for r in new_runs))
                        comment_id = ctx.jira.comment(ticket_key, body)
                        all_ids = sorted(known | {r["id"] for r in new_runs})
                        ctx.db.run("UPDATE flow_bug_tracker SET run_ids=%s, repeat_count=%s, updated_at=now()"
                                   " WHERE signature=%s", (all_ids, repeat_count, candidate["signature"]))
                        audit("jira", "flow_bug_repeat", ticket_key, str(run["id"]), run["fence"],
                              run["account_id"], True, comment_id,
                              {"signature": candidate["signature"], "repeat_count": repeat_count,
                               "new_run_ids": [r["id"] for r in new_runs]})
                        ctx.events.emit("flow_bug.repeated", ticket=ticket_key, project="TESTFLOW",
                                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"],
                                        node=run["node"], outcome="commented", reason=candidate["pattern"],
                                        data={"repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                        reports.append({"action": "repeated", "ticket": ticket_key,
                                        "repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                    continue

            if not candidate["creation_eligible"]:
                # The tracker keeps low-volume recurrences visible for open Bugs, but a closed Bug only
                # gets replaced after the normal repetition threshold or an aged ticket_stuck incident.
                continue

            run_lines = [f"- {item['id']} · {item['ticket']} · {item['step']} · node {item['node']} · release {item['release']}"
                         for item in candidate["runs"]]
            reason = ("at least three matching failed runs across at least two TESTFLOW tickets in 30 minutes"
                      if len(candidate["runs"]) >= 3 and len(candidate["tickets"]) >= 2 else
                      "a ticket_stuck incident for this error pattern remained open over 30 minutes")
            description = ("Automatically detected by the AI Flow master cycle.\n\n"
                           f"Fingerprint: {candidate['signature']}\n"
                           f"Pattern: {candidate['pattern']}\n"
                           f"Error kind: {candidate['error_kind']}\n"
                           f"Detection: {reason}.\n"
                           f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                           f"Steps: {', '.join(candidate['steps'])}\n"
                           f"Nodes: {', '.join(candidate['nodes'])}\n"
                           f"AI Flow releases: {', '.join(candidate['releases'])}\n"
                           "Related runs:\n" + "\n".join(run_lines) + "\n\n"
                           f"Suggested fix: {suggested_flow_fix(candidate)}")
            summary = f"[Flow bug] {candidate['pattern'].splitlines()[0]}"[:240]
            try:
                ticket_key = ctx.jira.create_issue("TESTFLOW", summary, description, issuetype="Bug",
                                                   labels=["flow-bug"])
            except Exception as exc:  # Keep the monitoring snapshot available and retry detection next cycle.
                audit("jira", "flow_bug_create", "", str(run["id"]), run["fence"], run["account_id"], False,
                      "Jira issue creation failed", {"signature": candidate["signature"], "error": type(exc).__name__})
                log.warning("could not create flow-bug for signature %s: %s",
                            candidate["signature"], type(exc).__name__)
                continue
            # Record the new Bug before any further Jira call, so a failure below never leads to a second Bug for
            # this signature. The status is refreshed from Jira on the next cycle; it is not Intake until moved.
            run_ids = sorted(item["id"] for item in candidate["runs"])
            ctx.db.run("INSERT INTO flow_bug_tracker (signature, ticket_key, run_ids, repeat_count, in_intake)"
                       " VALUES (%s,%s,%s,0,false) ON CONFLICT (signature) DO UPDATE SET ticket_key=EXCLUDED.ticket_key,"
                       " run_ids=EXCLUDED.run_ids, repeat_count=0, in_intake=false, updated_at=now()",
                       (candidate["signature"], ticket_key, run_ids))
            ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                       " VALUES (%s,'TESTFLOW','Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                       " type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                       " description=EXCLUDED.description, synced_at=now()",
                       (ticket_key, ["flow-bug"], summary, description))
            audit("jira", "flow_bug_create", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
                  "created", {"reason": reason, "signature": candidate["signature"], "run_ids": run_ids,
                              "tickets": candidate["tickets"], "labels": ["flow-bug"]})
            status = move_flow_bug_to_intake(run, ticket_key, candidate["signature"])
            ctx.events.emit("flow_bug.created", ticket=ticket_key, project="TESTFLOW", run_id=str(run["id"]),
                            fence=run["fence"], account=run["account_id"], node=run["node"],
                            outcome=status or "created", reason=candidate["pattern"],
                            data={"signature": candidate["signature"], "run_ids": run_ids,
                                  "tickets": candidate["tickets"]})
            reports.append({"action": "created", "ticket": ticket_key, "signature": candidate["signature"],
                            "run_ids": run_ids, "in_intake": status == "Intake"})
        return reports

    def move_flow_bug_to_intake(run: dict, ticket_key: str, signature: str) -> str | None:
        """Move a tracked flow-bug to Intake once; returns "Intake", or None when the move failed (retried later)."""
        try:
            status = ctx.jira.transition_to(ticket_key, "Intake")
            if status != "Intake":
                raise RuntimeError("flow-bug did not reach Intake")
        except Exception as exc:  # The tracker row keeps the key; the next master cycle retries only the move.
            audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], False,
                  "Intake transition failed", {"signature": signature, "error": type(exc).__name__})
            log.warning("could not move flow-bug %s to Intake: %s", ticket_key, type(exc).__name__)
            return None
        ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now() WHERE signature=%s", (signature,))
        ctx.db.run("UPDATE tickets SET status='Intake', synced_at=now() WHERE key=%s", (ticket_key,))
        audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
              "Intake", {"signature": signature})
        return "Intake"

    # --- health ---------------------------------------------------------------------------------------------
    @app.get("/v1/console/session")
    def console_session(request: Request) -> dict:
        user = (console_user(request, None) if ctx.settings.console_login else optional_console_user(request))
        if user is None:
            return {"authenticated": False}
        effective = user.view_as or user.role
        return {"authenticated": True, "user": user.subject, "role": effective,
                "role_label": ROLE_LABELS[effective], "roles": list(user.assigned_roles),
                "role_labels": [ROLE_LABELS[role] for role in user.assigned_roles],
                "permissions": sorted(user.permissions), "landing_page": landing_page((effective,)),
                "can_view_as": user.is_administrator, "viewing_as": user.view_as,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role]),
                                  "landing_page": landing_page((role,))} for role in CANONICAL_ROLES]}

    def validate_project_keys(project_keys: list[str] | None) -> list[str] | None:
        if project_keys is None:
            return None
        selected = list(dict.fromkeys(str(key).strip() for key in project_keys if str(key).strip()))
        unknown = sorted(set(selected) - set(ctx.repo().projects))
        if unknown:
            raise HTTPException(400, "unknown project: " + ", ".join(unknown))
        return selected

    @app.get("/v1/console/users")
    def console_users(request: Request) -> dict:
        console_user(request, "console.manage")
        rows = ctx.db.all("SELECT email,display_name,roles,project_keys,disabled,last_login_at,created_at"
                          " FROM console_users ORDER BY lower(display_name),email")
        audits = ctx.db.all("SELECT ts,actor,action,accepted,result,payload FROM write_audit"
                            " WHERE target='console' AND action IN ('user_add','user_update','user_disable',"
                            "'user_enable','view_as','view_as_stop') ORDER BY id DESC LIMIT 100")
        return {"users": rows, "projects": sorted(ctx.repo().projects), "audit": audits,
                "role_options": [{"id": role, "label": ROLE_LABELS[role],
                                  "permissions": sorted(ROLE_PERMISSIONS[role])} for role in CANONICAL_ROLES]}

    @app.post("/v1/console/users")
    def add_console_user(body: ConsoleUserCreateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", action="user_add")
        from .login import generate_password, hash_password, normalize_email
        try:
            email = normalize_email(body.email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        password = generate_password()
        try:
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
                    licensing.lock_resource_admission(connection)
                    people = connection.execute(
                        "SELECT count(*) AS n FROM console_users WHERE disabled=false").fetchone()["n"]
                    require_capacity("users", int(people), connection=connection)
                    created = connection.execute(
                        "INSERT INTO console_users (email,display_name,role,roles,project_keys,password_hash)"
                        " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (email) DO NOTHING RETURNING email",
                        (email, body.display_name.strip()[:200], roles[0], list(roles), project_keys,
                         hash_password(password))).fetchone()
                    if not created:
                        raise HTTPException(409, "a console user with that email already exists")
                    audit_in(connection, "console", "user_add", "", "", None, user.subject, True, "Person added",
                             {"email": email, "roles": list(roles), "project_keys": project_keys})
        except HTTPException:
            raise
        except Exception as exc:
            log.exception("console user could not be added")
            raise HTTPException(500, "person could not be added") from exc
        return {"email": email, "temporary_password": password}

    @app.put("/v1/console/users/{email}")
    def update_console_user(email: str, body: ConsoleUserUpdateBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console", action="user_update")
        from .login import normalize_email
        try:
            email = normalize_email(email)
            roles = normalize_roles(body.roles)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project_keys = validate_project_keys(body.project_keys)
        with ctx.db.pool.connection() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            current_roles = normalize_roles(current["roles"])
            if not current["disabled"] and "administrator" in current_roles and "administrator" not in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be removed")
            connection.execute("UPDATE console_users SET role=%s,roles=%s,project_keys=%s,updated_at=now() WHERE email=%s",
                               (roles[0], list(roles), project_keys, email))
            audit_in(connection, "console", "user_update", "", "", None, user.subject, True, "Person updated",
                     {"email": email, "roles": list(roles), "project_keys": project_keys})
        return {"updated": True}

    @app.patch("/v1/console/users/{email}/disabled")
    def set_console_user_disabled(email: str, body: ConsoleUserDisabledBody, request: Request) -> dict:
        user = console_user(request, "console.manage", target="console",
                            action="user_disable" if body.disabled else "user_enable")
        from .login import normalize_email
        try:
            email = normalize_email(email)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with ctx.db.pool.connection() as connection:
            connection.execute("LOCK TABLE console_users IN EXCLUSIVE MODE")
            current = connection.execute("SELECT roles,disabled FROM console_users WHERE email=%s FOR UPDATE",
                                         (email,)).fetchone()
            if not current:
                raise HTTPException(404, "console user not found")
            roles = normalize_roles(current["roles"])
            if body.disabled and not current["disabled"] and "administrator" in roles:
                others = connection.execute("SELECT count(*) AS n FROM console_users WHERE email<>%s AND disabled=false"
                                            " AND 'administrator'=ANY(roles)", (email,)).fetchone()["n"]
                if int(others) == 0:
                    raise HTTPException(409, "the last administrator cannot be disabled")
            connection.execute("UPDATE console_users SET disabled=%s,updated_at=now() WHERE email=%s",
                               (body.disabled, email))
            action = "user_disable" if body.disabled else "user_enable"
            audit_in(connection, "console", action, "", "", None, user.subject, True,
                     "Person disabled" if body.disabled else "Person enabled", {"email": email})
        return {"disabled": body.disabled}

    @app.post("/v1/console/view-as")
    def console_view_as(body: ViewAsBody, request: Request) -> JSONResponse:
        user = console_user(request, "console.manage", target="console", action="view_as")
        try:
            role = normalize_role(body.role)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from .login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret, view_as=role)
        audit("console", "view_as", "", "", None, user.subject, True, "Administrator viewing as role",
              {"role": role})
        response = JSONResponse({"viewing_as": role, "role_label": ROLE_LABELS[role]})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True,
                            secure=str(ctx.settings.public_url or "").lower().startswith("https://"),
                            samesite="strict", path="/")
        return response

    @app.post("/v1/console/view-as/stop")
    def console_stop_view_as(request: Request) -> JSONResponse:
        claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
        user = resolve_session_user(ctx.db, claims) if claims else None
        if not user or not user.is_administrator:
            raise HTTPException(403, "not available for your role")
        from .login import SESSION_TTL_S
        token = issue_session(user.subject, user.assigned_roles, int(utcnow().timestamp()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret)
        audit("console", "view_as_stop", "", "", None, user.subject, True, "Administrator returned to own role",
              {"previous_role": user.view_as or ""})
        response = JSONResponse({"viewing_as": None})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True,
                            secure=str(ctx.settings.public_url or "").lower().startswith("https://"),
                            samesite="strict", path="/")
        return response

    @app.post("/v1/support/bundles")
    def create_support_bundle(body: SupportBundleBody, request: Request) -> dict:
        creator = console_user(request, {"console.manage"}, target="support_bundle", action="create")
        ticket_key = body.ticket_key.strip().upper() or None
        try:
            archive, inventory = support_bundle.build_support_bundle(ctx, ticket_key)
        except KeyError as exc:
            raise HTTPException(404, "unknown ticket") from exc
        except ValueError as exc:
            log.warning("support bundle scan blocked; sensitive content remained after sanitization")
            raise HTTPException(422, "bundle scan failed; no bundle was offered") from exc
        bundle_id = secrets.token_urlsafe(24)
        key = ctx.leases._k("support_bundle", bundle_id)
        payload = json.dumps({"archive": base64.b64encode(archive).decode("ascii"), "inventory": inventory,
                              "creator": creator.subject}, separators=(",", ":"))
        try:
            ctx.leases.r.set(key, payload, ex=600)
        except Exception as exc:  # noqa: BLE001 - do not return an unreviewable or non-shared in-memory bundle
            log.warning("temporary support bundle storage is unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "temporary bundle storage is unavailable") from exc
        return {"bundle_id": bundle_id, "inventory": inventory, "download_url": f"/v1/support/bundles/{bundle_id}/download"}

    @app.get("/v1/support/bundles/{bundle_id}/download")
    def download_support_bundle(bundle_id: str, request: Request) -> Response:
        user = console_user(request, {"console.manage"})
        key = ctx.leases._k("support_bundle", bundle_id)
        try:
            stored = ctx.leases.r.get(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("temporary support bundle storage is unavailable: %s", type(exc).__name__)
            raise HTTPException(503, "temporary bundle storage is unavailable") from exc
        if not stored:
            raise HTTPException(404, "bundle expired or unknown")
        item = json.loads(stored)
        if not item.get("creator") or not secrets.compare_digest(str(item["creator"]), user.subject):
            raise HTTPException(403, "bundle belongs to the user who created it")
        ticket_key = item["inventory"].get("ticket_key")
        filename = f"support-bundle-{ticket_key}.zip" if ticket_key else "support-bundle.zip"
        archive = base64.b64decode(item["archive"], validate=True)
        return Response(archive, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    # --- per-person console layouts (layouts.py, docs/v4/console-layouts.md) ---------------------------------
    def layout_page(page: str) -> str:
        if page not in layouts.PAGES:
            raise HTTPException(404, "unknown console page")
        return page

    @app.get("/v1/console/layouts")
    def console_layouts(request: Request) -> dict:
        user = console_user(request, None)
        return layouts.overview(ctx.db, user.subject, user.role)

    @app.put("/v1/console/layouts/{page}")
    def console_layout_save(page: str, body: layouts.LayoutBody, request: Request) -> dict:
        # Layouts are personal preferences, like reads, and remain available if the licence expires.
        user = console_user(request, None)
        try:
            blocks = layouts.normalise(layout_page(page), [b.model_dump() for b in body.blocks])
        except layouts.LayoutError as exc:
            raise HTTPException(400, str(exc)) from None
        layouts.save(ctx.db, user.subject, page, blocks)
        return layouts.page_view(page, user.role, blocks)

    @app.delete("/v1/console/layouts/{page}")
    def console_layout_reset(page: str, request: Request) -> dict:
        user = console_user(request, None)
        layouts.reset(ctx.db, user.subject, layout_page(page))
        return layouts.page_view(page, user.role, None)

    def notification_admin(request: Request, action: str = "read") -> ConsoleUser:
        return console_user(request, {"notifications.manage"}, target="notifications", action=action)

    async def notification_json(request: Request) -> dict:
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid notification settings") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "invalid notification settings")
        return payload

    @app.get("/v1/notifications")
    def notification_settings(request: Request) -> dict:
        notification_admin(request)
        return notify.public_settings(ctx.db)

    @app.put("/v1/notifications/channels")
    async def notification_channel_save(request: Request) -> dict:
        notification_admin(request, "write")
        try:
            channel = notify.save_channel(ctx.db, await notification_json(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"channel": channel}

    @app.delete("/v1/notifications/channels/{channel_id}")
    def notification_channel_delete(channel_id: int, request: Request) -> dict:
        notification_admin(request, "write")
        if not notify.delete_channel(ctx.db, channel_id):
            raise HTTPException(404, "notification channel was not found")
        return {"deleted": True}

    @app.put("/v1/notifications/preferences")
    async def notification_preferences_save(request: Request) -> dict:
        notification_admin(request, "write")
        try:
            preferences = notify.save_preferences(ctx.db, await notification_json(request))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"preferences": preferences}

    @app.post("/v1/notifications/channels/{channel_id}/test")
    def notification_channel_test(channel_id: int, request: Request) -> dict:
        notification_admin(request, "write")
        try:
            return notify.send_test(ctx, channel_id)
        except ValueError:
            raise HTTPException(404, "notification channel was not found") from None

    @app.get("/healthz")
    def healthz() -> dict:
        ctx.db.one("SELECT 1 AS ok")
        ctx.leases.r.ping()
        return {"ok": True, "version": VERSION}

    # --- licence and metered usage ---------------------------------------------------------------------------
    @app.get("/v1/licensing")
    def licensing_state(request: Request) -> dict:
        console_user(request, "licensing.manage")
        state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        state["usage"] = licensing.resource_counts(ctx)
        state["sync_warnings"] = list(app.state.license_sync_warnings)
        return state

    @app.get("/v1/licensing/status")
    def licensing_status(request: Request) -> dict:
        console_user(request, "console.read")
        state = licensing.snapshot(ctx.db, ctx.settings.license_public_key)
        return {key: state[key] for key in ("status", "reason", "plan", "expires_at", "grace_until", "read_only")}

    @app.post("/v1/licensing/activate")
    def activate_license(body: LicenseKeyBody, request: Request) -> dict:
        console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            return licensing.activate_online(ctx.db, body.key, ctx.settings.license_service_url,
                                             ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/v1/licensing/offline")
    def activate_license_offline(body: OfflineLicenseBody, request: Request) -> dict:
        console_user(request, "licensing.manage", target="licensing", action="license_activate")
        try:
            return licensing.activate(ctx.db, body.license_file, ctx.settings.license_public_key, "offline")
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/v1/licensing/limits")
    def update_license_limits(body: PlanLimitsBody, request: Request) -> dict:
        console_user(request, "licensing.manage", target="licensing", action="license_limits")
        try:
            return licensing.save_limits(ctx.db, body.plan, body.limits, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/v1/licensing/usage")
    def licensing_usage(request: Request) -> dict:
        console_user(request, "console.read")
        return {"rows": licensing.usage_report(ctx.db)}

    @app.get("/v1/licensing/usage.csv")
    def licensing_usage_csv(request: Request) -> PlainTextResponse:
        console_user(request, "console.read")
        content = licensing.usage_csv(licensing.usage_report(ctx.db))
        return PlainTextResponse(content, media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=ai-flow-usage.csv"})

    # --- Owner-managed GitHub App connection ----------------------------------------------------------------
    def github_secret_store() -> GitHubSecretStore:
        return GitHubSecretStore(ctx.settings.github_app_dir)

    def audit_github_repository_changes(gateway: GitHubApp, repositories: list[dict[str, Any]], actor: str,
                                       source: str) -> None:
        store = github_secret_store()
        if not hasattr(gateway, "meta") or not store.active_exists:
            return
        names = sorted({str(item.get("full_name") or "") for item in repositories if item.get("full_name")})
        previous = store.update_repository_snapshot(names)
        if hasattr(gateway, "_refresh_config"):
            gateway._refresh_config()
        if previous is None:
            audit("github", "repository_snapshot", "", "", None, actor, True,
                  "GitHub installation repositories recorded", {"count": len(names), "source": source})
            return
        old_by_key = {name.lower(): name for name in previous}
        new_by_key = {name.lower(): name for name in names}
        added = sorted(new_by_key[key] for key in new_by_key.keys() - old_by_key.keys())
        removed = sorted(old_by_key[key] for key in old_by_key.keys() - new_by_key.keys())
        if added or removed:
            audit("github", "repositories_changed", "", "", None, actor, True,
                  "GitHub installation repository access changed",
                  {"added": added, "removed": removed, "source": source})
            ctx.events.emit("github.repositories_changed", account=getattr(gateway, "org", ""),
                            outcome="updated", reason=source, data={"added": added, "removed": removed})

    def github_install_url(slug: str) -> str:
        return f"https://github.com/apps/{quote(slug, safe='-')}/installations/new"

    def github_installation_settings(account_login: str, account_type: str, installation_id: int) -> str:
        if account_type.lower() == "organization":
            return (f"https://github.com/organizations/{quote(account_login, safe='-')}/settings/"
                    f"installations/{installation_id}")
        return f"https://github.com/settings/installations/{installation_id}"

    def github_public_base() -> str:
        value = str(ctx.settings.public_url or "").rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise HTTPException(503, "set AIFLOW4_PUBLIC_URL to the HTTPS URL owners use to open AI Flow")
        return value

    def pending_gateway(store: GitHubSecretStore, pending_id: str, meta: dict[str, Any]) -> GitHubApp:
        directory = store._pending_dir(pending_id)
        return GitHubApp(str(directory), str(meta.get("account_login") or ctx.settings.github_org))

    def complete_github_connection(store: GitHubSecretStore, pending_id: str, installation_id: int,
                                   actor: str, source: str) -> dict[str, Any]:
        record, meta, private_key = store.read_pending(pending_id)
        if not private_key or record.get("phase") not in {"manifest_setup", "import"}:
            raise ValueError("GitHub App setup is incomplete")
        gateway = pending_gateway(store, pending_id, meta)
        validate_github_app_permissions(gateway.app_details())
        installation = gateway.installation_details(installation_id)
        if int(installation.get("id") or 0) != installation_id:
            raise ValueError("GitHub installation could not be verified")
        account = installation.get("account") or {}
        account_login = str(account.get("login") or "")
        account_type = str(account.get("type") or "")
        if not account_login:
            raise ValueError("GitHub installation account is missing")
        expected = str(record.get("expected_owner") or "")
        if expected and account_login.lower() != expected.lower():
            raise ValueError("GitHub installation account does not match the selected organization")
        if installation.get("suspended_at"):
            raise ValueError("GitHub App installation is suspended")

        # Validate the installation token and selected repository list before activating the key.
        active_meta = app_metadata(meta, account_login=account_login, account_type=account_type,
                                   installation_id=installation_id)
        store.update_pending(pending_id, record, active_meta, private_key)
        gateway = pending_gateway(store, pending_id, active_meta)
        repositories = gateway.installation_repositories()
        active_meta["repository_names"] = sorted({str(item["full_name"]) for item in repositories
                                                  if item.get("full_name")})
        target = str(record.get("connection") or "default")
        if target == "default" or (target == "new" and not store.active_exists):
            connection_id = DEFAULT_ID
            store.save_active(active_meta, private_key)
            ctx.github = GitHubApp(ctx.settings.github_app_dir, account_login)
            ctx.settings.github_org = account_login
        else:
            connection_id = github_connections(ctx).save_app(None if target == "new" else target, "",
                                                             active_meta, private_key)
        store.delete_pending(pending_id)
        audit("github", "connect", "", "", None, actor, True, "GitHub App connected",
              {"app_id": str(active_meta.get("id") or ""), "installation_id": installation_id,
               "account": account_login, "repositories": len(repositories), "source": source,
               "connection": connection_id})
        ctx.events.emit("github.connected", account=account_login, outcome="connected", reason=source)
        return {"account": account_login, "account_type": account_type, "installation_id": installation_id,
                "connection": connection_id,
                "repositories": len(repositories), "settings_url": github_installation_settings(
                    account_login, account_type, installation_id)}

    @app.get("/v1/integrations/github")
    def github_connection_status(request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"})
        gateway = github_gateway(ctx)
        if not gateway:
            return {"connected": False, "app": None, "account": None, "installation_id": None,
                    "repository_count": 0, "settings_url": ""}
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
        account_type = str(meta.get("account_type") or "Organization")
        try:
            repositories = gateway.installation_repositories()
            audit_github_repository_changes(gateway, repositories, user.subject, "integration_refresh")
        except Exception as exc:  # provider response may contain credentials; return only a generic status
            log.warning("GitHub installation status failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load GitHub installation status") from exc
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        return {"connected": True,
                "app": {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                        "slug": str(meta.get("slug") or "")},
                "account": {"login": account, "type": account_type},
                "installation_id": int(installation_id) if installation_id else None,
                "repository_count": len(repositories),
                "settings_url": github_installation_settings(account, account_type, int(installation_id))
                if installation_id else ""}

    def target_connection(value: Any) -> str:
        """Where a GitHub App being connected goes: the default one, a new connection, or an existing id."""
        text = str(value or "default")
        if text in ("default", "new") or (text != DEFAULT_ID and github_connections(ctx).meta(text)):
            return text
        raise HTTPException(422, "unknown GitHub connection")

    def connection_view(conn: Connection) -> dict[str, Any]:
        """One row of the Integrations list: account, credential type, status and reachable repositories."""
        meta = getattr(conn.gateway, "meta", {}) or {}
        view: dict[str, Any] = {**conn.summary(), "status": "connected", "repositories": [], "warning": "",
                                "app": None, "settings_url": ""}
        if conn.kind == "token":
            view["warning"] = ("An access token reaches more than a GitHub App, has no bot identity and cannot "
                               "be limited per run: it stays in Command Center and is never given to a node. "
                               "Prefer a GitHub App.")
        else:
            view["app"] = {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                           "slug": str(meta.get("slug") or "")}
            installation = meta.get("installation_id") or getattr(conn.gateway, "_inst", None)
            if installation and conn.account:
                view["settings_url"] = github_installation_settings(conn.account, conn.account_type or "Organization",
                                                                    int(installation))
        try:
            view["repositories"] = sorted(
                str(r.get("full_name")) for r in conn.gateway.installation_repositories()
                if str(r.get("full_name") or "").split("/", 1)[0].lower() == conn.account.lower())
        except Exception as exc:  # provider text may carry credentials; show only a generic status
            log.warning("GitHub connection %s status failed (%s)", conn.id, type(exc).__name__)
            view["status"] = "error"
        return view

    def find_connection(connection_id: str) -> Connection:
        conn = next((c for c in github_connection_list(ctx) if c.id == connection_id), None)
        if conn is None:
            raise HTTPException(404, "unknown GitHub connection")
        return conn

    @app.get("/v1/integrations/github/connections")
    def github_connections_status(request: Request) -> dict[str, Any]:
        console_user(request, {"integrations.manage"})
        return {"connections": [connection_view(c) for c in github_connection_list(ctx)],
                "recommendation": "Use a GitHub App; an access token is broader, has no bot identity and cannot "
                                  "be limited to one repository per run."}

    @app.post("/v1/integrations/github/connections/token")
    def github_add_token(body: GitHubConnectionTokenBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="add_token_connection")
        account = body.account.strip()
        if account and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", account):
            audit("github", "add_token_connection", "", "", None, user.subject, False, "invalid account")
            raise HTTPException(422, "enter a valid GitHub account or organization login")
        try:
            gateway = verify_token(body.token.strip(), account)
        except Exception as exc:
            log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "add_token_connection", "", "", None, user.subject, False, "token could not be verified")
            raise HTTPException(422, "GitHub did not accept this token for that account") from exc
        label = body.label.strip() or f"{gateway.org} access token"
        connection_id = github_connections(ctx).add_token(label, body.token.strip(), gateway.org,
                                                           gateway.meta.get("account_type", ""))
        audit("github", "add_token_connection", "", "", None, user.subject, True, "access token connection added",
              {"connection": connection_id, "account": gateway.org})
        ctx.events.emit("github.connected", account=gateway.org, outcome="connected", reason="token")
        return connection_view(find_connection(connection_id))

    @app.post("/v1/integrations/github/connections/{connection_id}/test")
    def github_test_connection(connection_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="test_connection")
        view = connection_view(find_connection(connection_id))
        audit("github", "test_connection", "", "", None, user.subject, view["status"] == "connected",
              view["status"], {"connection": connection_id, "repositories": len(view["repositories"])})
        return view

    @app.put("/v1/integrations/github/connections/{connection_id}/token")
    def github_replace_token(connection_id: str, body: GitHubConnectionTokenBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="replace_token_connection")
        conn = find_connection(connection_id)
        if conn.kind != "token":
            raise HTTPException(409, "only an access token connection takes a new token; reconnect the App instead")
        try:
            gateway = verify_token(body.token.strip(), conn.account)
        except Exception as exc:
            log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "replace_token_connection", "", "", None, user.subject, False,
                  "token could not be verified", {"connection": connection_id})
            raise HTTPException(422, "GitHub did not accept this token for that account") from exc
        github_connections(ctx).replace_token(connection_id, body.token.strip(), gateway.org,
                                              gateway.meta.get("account_type", ""))
        audit("github", "replace_token_connection", "", "", None, user.subject, True, "access token replaced",
              {"connection": connection_id, "account": gateway.org})
        return connection_view(find_connection(connection_id))

    @app.delete("/v1/integrations/github/connections/{connection_id}")
    def github_remove_connection(connection_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="remove_connection")
        find_connection(connection_id)
        registry = github_connections(ctx)
        using = sorted(repo for repo, assigned in registry.assignments().items() if assigned == connection_id)
        if using:
            audit("github", "remove_connection", "", "", None, user.subject, False,
                  "connection is assigned to repositories", {"connection": connection_id, "repositories": using})
            raise HTTPException(409, "reassign these repositories first: " + ", ".join(using))
        if connection_id == DEFAULT_ID:
            for name in ("app.json", "app.pem"):
                (Path(ctx.settings.github_app_dir) / name).unlink(missing_ok=True)
            ctx.github = None
        else:
            registry.remove(connection_id)
        audit("github", "remove_connection", "", "", None, user.subject, True, "connection removed",
              {"connection": connection_id})
        return {"removed": connection_id}

    @app.get("/v1/integrations/github/manifest/start", response_class=HTMLResponse)
    def github_manifest_start(organization: str, request: Request, connection: str = "default") -> HTMLResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="manifest_start")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", organization):
            audit("github", "manifest_start", "", "", None, user.subject, False, "invalid GitHub organization")
            raise HTTPException(422, "enter a valid GitHub organization login")
        base = github_public_base()
        store = github_secret_store()
        state = store.new_id()
        store.create_pending(state, {"phase": "manifest", "actor": user.subject, "connection": target_connection(connection),
                                     "expected_owner": organization, "source": "manifest"})
        audit("github", "manifest_start", "", "", None, user.subject, True, "GitHub App manifest started",
              {"organization": organization})
        callback = base + "/v1/integrations/github/manifest/callback"
        setup = callback.removesuffix("/manifest/callback") + "/setup?state=" + state
        manifest = {"name": f"AI Flow {organization}", "url": base + "/cc", "description": "AI Flow GitHub integration",
                    "redirect_url": callback, "setup_url": setup, "setup_on_update": False, "public": False,
                    "default_events": [],
                    "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read",
                                            "actions": "read",
                                            "checks": "read", "statuses": "read", "workflows": "write",
                                            "administration": "write"}}
        action = f"https://github.com/organizations/{quote(organization, safe='-')}/settings/apps/new?state={state}"
        encoded = html.escape(json.dumps(manifest, separators=(",", ":")), quote=True)
        action_attr = html.escape(action, quote=True)
        body = ("<!doctype html><html><head><meta charset='utf-8'><title>Connect GitHub</title></head><body>"
                "<p>Continue to GitHub to create the AI Flow App.</p>"
                f"<form id='manifest' action='{action_attr}' method='post'>"
                f"<input type='hidden' name='manifest' value='{encoded}'>"
                "<button type='submit'>Continue to GitHub</button></form>"
                "<script>document.getElementById('manifest').submit()</script></body></html>")
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/v1/integrations/github/manifest/callback")
    def github_manifest_callback(code: str, state: str) -> RedirectResponse:
        store = github_secret_store()
        try:
            record, _, _ = store.read_pending(state)
            if record.get("phase") != "manifest":
                raise ValueError("GitHub manifest state is invalid")
            if not re.fullmatch(r"[A-Za-z0-9]+", code):
                raise ValueError("GitHub manifest response is invalid")
            with httpx.Client(base_url="https://api.github.com", timeout=30,
                              headers={"Accept": "application/vnd.github+json",
                                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"}) as client:
                response = client.post(f"/app-manifests/{quote(code, safe='')}/conversions")
            if response.status_code not in (200, 201):
                raise ValueError("GitHub manifest could not be completed")
            converted = response.json()
            pem = converted.get("pem")
            if not converted.get("id") or not converted.get("slug") or not isinstance(pem, str) or not pem.startswith("-----BEGIN "):
                raise ValueError("GitHub manifest response is incomplete")
            app_owner = converted.get("owner") or {}
            if (str(app_owner.get("login") or "").lower() != str(record.get("expected_owner") or "").lower()
                    or str(app_owner.get("type") or "").lower() != "organization"):
                raise ValueError("GitHub App owner does not match the selected organization")
            record["phase"] = "manifest_setup"
            meta = app_metadata(converted)
            store.update_pending(state, record, meta, pem)
            actor = str(record.get("actor") or "github-manifest")
            audit("github", "manifest_created", "", "", None, actor, True, "GitHub App created",
                  {"app_id": str(meta.get("id") or ""), "organization": record.get("expected_owner", "")})
            return RedirectResponse(github_install_url(str(meta["slug"])), status_code=303)
        except Exception as exc:  # no provider response or callback credential is copied into logs
            log.warning("GitHub manifest callback failed (%s)", type(exc).__name__)
            actor = "github-manifest"
            try:
                record, _, _ = store.read_pending(state)
                actor = str(record.get("actor") or actor)
                store.delete_pending(state)
            except Exception:
                pass
            audit("github", "manifest_created", "", "", None, actor, False, "GitHub manifest setup failed")
            return RedirectResponse("/integrations?github=error", status_code=303)

    @app.get("/v1/integrations/github/setup")
    def github_install_setup(installation_id: int, state: str, setup_action: str = "install") -> RedirectResponse:
        store = github_secret_store()
        actor = "github-setup"
        try:
            record, _, _ = store.read_pending(state)
            actor = str(record.get("actor") or actor)
            if record.get("phase") != "manifest_setup":
                raise ValueError("GitHub setup state is invalid")
            complete_github_connection(store, state, installation_id, actor, "manifest")
            return RedirectResponse("/integrations?github=connected", status_code=303)
        except Exception as exc:
            log.warning("GitHub installation callback failed (%s)", type(exc).__name__)
            try:
                store.delete_pending(state)
            except Exception:
                pass
            audit("github", "connect", "", "", None, actor, False, "GitHub installation could not be verified",
                  {"installation_id": installation_id, "source": "manifest", "setup_action": setup_action})
            return RedirectResponse("/integrations?github=error", status_code=303)

    @app.post("/v1/integrations/github/imports")
    async def github_import_app(request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="import_app")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 25_000:
                audit("github", "import_app", "", "", None, user.subject, False, "private key upload too large")
                raise HTTPException(413, "private key file is too large")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid App import request")
            raise HTTPException(422, "select a GitHub App ID and private key file")
        if not isinstance(payload, dict):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid App import request")
            raise HTTPException(422, "select a GitHub App ID and private key file")
        app_id, pem = payload.get("app_id"), payload.get("private_key")
        if type(app_id) is not int or app_id < 1:
            audit("github", "import_app", "", "", None, user.subject, False, "invalid GitHub App id")
            raise HTTPException(422, "enter a valid GitHub App ID")
        if not isinstance(pem, str) or (len(pem) < 100 or len(pem) > 20_000 or "\x00" in pem or not pem.startswith("-----BEGIN ")
                or "PRIVATE KEY-----" not in pem or not pem.rstrip().endswith("-----END PRIVATE KEY-----")
                and not pem.rstrip().endswith("-----END RSA PRIVATE KEY-----")):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid private key file",
                  {"app_id": app_id})
            raise HTTPException(422, "select a valid GitHub App private key file")
        store = github_secret_store()
        pending_id = store.new_id()
        record = {"phase": "import", "actor": user.subject, "source": "import", "app": {"id": app_id},
                  "connection": target_connection(payload.get("connection"))}
        store.create_pending(pending_id, record, pem)
        try:
            _, meta, _ = store.read_pending(pending_id)
            gateway = pending_gateway(store, pending_id, meta)
            details = gateway.app_details()
            if int(details.get("id") or 0) != app_id:
                raise ValueError("GitHub App id did not match")
            validate_github_app_permissions(details)
            if not details.get("slug"):
                raise ValueError("GitHub App slug is missing")
            record["app"] = app_metadata(details)
            store.update_pending(pending_id, record, app_metadata(details), pem)
            audit("github", "import_app", "", "", None, user.subject, True, "GitHub App credentials verified",
                  {"app_id": app_id})
            return {"pending_id": pending_id,
                    "app": {"id": str(details.get("id") or ""), "name": str(details.get("name") or "GitHub App"),
                            "slug": str(details.get("slug") or "")},
                    "install_url": github_install_url(str(details.get("slug") or ""))}
        except Exception as exc:
            store.delete_pending(pending_id)
            log.warning("GitHub App import could not be verified (%s)", type(exc).__name__)
            audit("github", "import_app", "", "", None, user.subject, False, "GitHub App credentials could not be verified",
                  {"app_id": app_id})
            raise HTTPException(422, "could not verify this GitHub App and private key") from exc

    @app.get("/v1/integrations/github/imports/{pending_id}/installations")
    def github_import_installations(pending_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="list_import_installations")
        store = github_secret_store()
        stage = "load pending App"
        try:
            record, meta, _ = store.read_pending(pending_id)
            if record.get("phase") != "import":
                raise ValueError("GitHub App import state is invalid")
            stage = "query GitHub installations"
            gateway = pending_gateway(store, pending_id, meta)
            installations = gateway.app_installations()
            stage = "format GitHub installations"
            accounts = []
            for item in installations:
                account = item.get("account") or {}
                if account.get("login"):
                    accounts.append({"id": int(item["id"]), "login": str(account["login"]),
                                     "type": str(account.get("type") or ""),
                                     "repository_selection": str(item.get("repository_selection") or "selected")})
            return {"app": {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                            "slug": str(meta.get("slug") or "")}, "installations": accounts,
                    "install_url": github_install_url(str(meta.get("slug") or ""))}
        except Exception as exc:
            log.warning("GitHub App installations could not be listed (%s, stage=%s)", type(exc).__name__, stage)
            raise HTTPException(502, "could not load installations for this GitHub App") from exc

    @app.get("/v1/integrations/github/imports/{pending_id}/install")
    def github_import_install(pending_id: str, request: Request) -> RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_app_install")
        try:
            record, meta, _ = github_secret_store().read_pending(pending_id)
            if record.get("phase") != "import" or not meta.get("slug"):
                raise ValueError("GitHub App import state is invalid")
        except Exception as exc:
            audit("github", "open_app_install", "", "", None, user.subject, False, "GitHub App import expired")
            raise HTTPException(404, "GitHub App import expired; start again") from exc
        audit("github", "open_app_install", "", "", None, user.subject, True, "GitHub App installation opened",
              {"app_id": str(meta.get("id") or "")})
        return RedirectResponse(github_install_url(str(meta["slug"])), status_code=303)

    @app.post("/v1/integrations/github/imports/{pending_id}/connect")
    def github_import_connect(pending_id: str, body: GitHubInstallationBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="connect_imported_app")
        try:
            result = complete_github_connection(github_secret_store(), pending_id, body.installation_id,
                                               user.subject, "import")
            return {"connected": True, **result}
        except Exception as exc:
            log.warning("GitHub imported App connection failed (%s)", type(exc).__name__)
            audit("github", "connect", "", "", None, user.subject, False,
                  "GitHub installation could not be verified",
                  {"installation_id": body.installation_id, "source": "import"})
            raise HTTPException(422, "could not connect this GitHub installation") from exc

    @app.get("/v1/integrations/github/installation-settings")
    def github_open_installation_settings(request: Request) -> RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_installation_settings")
        gateway = github_gateway(ctx)
        if not gateway:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub App is not connected")
            raise HTTPException(503, "GitHub App is not connected")
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        if not installation_id:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub installation is not configured")
            raise HTTPException(503, "GitHub installation is not configured")
        account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
        account_type = str(meta.get("account_type") or "Organization")
        url = github_installation_settings(account, account_type, int(installation_id))
        audit("github", "open_installation_settings", "", "", None, user.subject, True,
              "GitHub repository settings opened",
              {"app_id": str(meta.get("id") or ""), "installation_id": int(installation_id), "account": account})
        return RedirectResponse(url, status_code=303)

    # --- Projects screen: owner-managed project definitions in aiflow-control -------------------------------
    required_project_statuses = ["Backlog", "Intake", "To Do", "In Progress", "In Verify", "Needs owner", "Done"]

    def jira_site() -> str:
        return str(getattr(ctx.jira, "site", "") or ctx.settings.jira_site).rstrip("/")

    def project_key_from_input(value: str, site: str) -> str:
        """Accept a Jira project key or a URL for the selected connected site."""
        raw = value.strip()
        if raw.startswith(("http://", "https://")):
            parsed = urlsplit(raw)
            selected = urlsplit(site)
            if not parsed.hostname or parsed.hostname.lower() != (selected.hostname or "").lower():
                raise HTTPException(422, "Jira project URL must belong to the selected connected site")
            parts = [part for part in parsed.path.split("/") if part]
            candidate = ""
            for index, part in enumerate(parts[:-1]):
                if part in {"projects", "browse"}:
                    candidate = parts[index + 1]
                    break
            if not candidate:
                raise HTTPException(422, "Jira project URL does not contain a project key")
            raw = candidate.split("-", 1)[0]
        key = raw.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key):
            raise HTTPException(422, "Jira project must be a project key or project URL")
        return key

    def github_catalog(actor: str, source: str) -> list[dict[str, Any]]:
        """Repositories of every connection, each with the connections that reach it and the one it uses."""
        connections = github_connection_list(ctx)
        assigned = github_connections(ctx).assignments()
        repos: dict[str, dict[str, Any]] = {}
        failures: list[Exception] = []
        for conn in connections:
            try:
                rows = conn.gateway.installation_repositories()
                if conn.id == DEFAULT_ID:
                    audit_github_repository_changes(conn.gateway, rows, actor, source)
            except Exception as exc:  # one broken connection must not hide the others
                log.warning("GitHub connection %s repositories failed (%s)", conn.id, type(exc).__name__)
                failures.append(exc)
                continue
            for item in rows:
                name = str(item.get("full_name") or "")
                if name.split("/", 1)[0].lower() != conn.account.lower():
                    continue
                repos.setdefault(name.lower(), {**item, "connections": []})["connections"].append(conn.id)
        if connections and len(failures) == len(connections):
            raise failures[0]
        for key, item in repos.items():
            assigned_connection = assigned.get(key, "")
            item["assigned_connection"] = assigned_connection
            item["connection"] = assigned_connection if assigned_connection in item["connections"] else (
                "" if assigned_connection else item["connections"][0])
        return sorted(repos.values(), key=lambda row: str(row["full_name"]).lower())

    def connection_summaries() -> list[dict[str, str]]:
        return [conn.summary() for conn in github_connection_list(ctx)]

    def project_preflight(body: ProjectPreflightBody, actor: str = "owner") -> dict[str, Any]:
        if not ctx.jira:
            raise HTTPException(503, "Jira is not connected")
        if not github_connection_list(ctx):
            raise HTTPException(503, "GitHub is not connected")
        site = jira_site()
        if body.site.rstrip("/") != site:
            raise HTTPException(422, "selected Jira site is not connected")
        key = project_key_from_input(body.project, site)
        if body.stack and body.stack not in STACK_TOOLS:
            raise HTTPException(422, "unsupported project stack")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", body.base) or ".." in body.base:
            raise HTTPException(422, "invalid base branch")
        org, slash, repo_name = body.repo.partition("/")
        if not slash or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo_name) or not re.fullmatch(r"[A-Za-z0-9_.-]+", org):
            raise HTTPException(422, "select a repository accessible to a connected GitHub account")

        try:
            jira_statuses = ctx.jira.project_statuses(key)
            catalog = github_catalog(actor, "project_preflight")
            entry = next((item for item in catalog if str(item["full_name"]).lower() == body.repo.lower()), None)
            if entry is None or not entry["connection"]:
                raise HTTPException(422, "repository is not accessible to any connected GitHub account")
            github = github_gateway(ctx, body.repo)
            branch_exists = bool(github.branch_exists(repo_name, body.base))
        except HTTPException:
            raise
        except Exception as exc:  # provider details can contain request metadata; keep them out of the UI
            log.warning("project preflight failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not complete Jira and GitHub project checks") from exc

        missing_statuses = [status for status in required_project_statuses if status not in jira_statuses]
        repo = ctx.repo()
        node_rows = {row["name"]: row for row in ctx.db.all("SELECT name,status,tools FROM nodes")}
        node_names = sorted(set(repo.nodes) | set(node_rows))
        required_tools = tools_for_stack(body.stack)
        node_results = []
        for name in node_names:
            row = node_rows.get(name) or {}
            tools = row.get("tools") or {}
            gaps = toolchain_gaps(tools, required_tools, repo.versions, [body.stack] if body.stack else (),
                                  require_pins=bool(body.stack))
            absent = missing_tools(tools, required_tools)
            gaps = list(dict.fromkeys(absent + gaps))
            node_results.append({"name": name, "status": row.get("status", "unknown"),
                                 "ready": not gaps, "missing_tools": gaps})
        return {"project": key, "site": site, "missing_statuses": missing_statuses,
                "branch_exists": branch_exists, "nodes": node_results, "required_tools": required_tools,
                "ok": not missing_statuses and branch_exists}

    def refresh_projects() -> bool:
        if not ctx.refresh_repo:
            return True
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception as exc:  # RepoCache will retry its pull on a later refresh
                log.warning("project control repo refresh failed (%s)", type(exc).__name__)
                return False
        return True

    def project_audit(action: str, key: str, actor: str, accepted: bool, result: str,
                      payload: dict[str, Any] | None = None) -> None:
        audit("aiflow-control", action, key, "", None, actor, accepted, result,
              payload or {"project": key})

    def ticket_epic(ticket: dict[str, Any]) -> str:
        """Resolve a ticket's parent chain to its Jira epic key."""
        if ticket.get("type") == "Epic":
            return str(ticket.get("key") or "")
        parent = str(ticket.get("parent_key") or "")
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            ancestor = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (parent,))
            if not ancestor:
                return parent
            if ancestor["type"] == "Epic":
                return parent
            parent = str(ancestor.get("parent_key") or "")
        return ""

    @app.get("/v1/projects")
    def managed_projects(request: Request) -> dict:
        user = (console_user(request, "projects.manage") if ctx.settings.console_login
                else optional_console_user(request))
        repo = ctx.repo()
        counts = {row["project"]: int(row["count"]) for row in
                  ctx.db.all("SELECT project,count(*) AS count FROM tickets GROUP BY project")}
        probes = tooling.probe_report(ctx)
        projects = [{"key": project.key, "repo": project.repo,
                              "repositories": project.configured_repositories,
                              "status": "paused" if project.paused else "active",
                              "priority": project.priority, "ticket_count": counts.get(project.key, 0),
                              "base": project.base, "stack": project.stack,
                              "checks": probes.get(project.key, {})}
                    for project in sorted(repo.projects.values(), key=lambda item: item.key)]
        if user and user.project_keys is not None:
            projects = [project for project in projects if project["key"] in user.project_keys]
        return {"projects": projects}

    @app.get("/v1/projects/catalog")
    def project_catalog(request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_catalog")
        if not ctx.jira or not github_connection_list(ctx):
            raise HTTPException(503, "Jira and GitHub must be connected before adding a project")
        try:
            jira_projects = ctx.jira.projects()
            github_repos = github_catalog(user.subject, "project_catalog")
        except Exception as exc:
            log.warning("project catalog could not be loaded (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load connected Jira projects and GitHub repositories") from exc
        return {"jira_sites": [jira_site()], "jira_projects": jira_projects,
                "github_repos": github_repos, "connections": connection_summaries(),
                "stacks": sorted(STACK_TOOLS), "required_statuses": required_project_statuses}

    def jira_project_epics(key: str) -> list[dict[str, str]]:
        """Read the current Jira epics for the project instead of relying on the periodic ticket mirror."""
        issues = ctx.jira.search(f'project = "{key}" AND issuetype = Epic ORDER BY key')
        out = []
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_type = (fields.get("issuetype") or {}).get("name") or ""
            if issue_type and issue_type.lower() != "epic":
                continue
            issue_key = str(issue.get("key") or "")
            if issue_key:
                out.append({"key": issue_key, "summary": str(fields.get("summary") or issue_key),
                            "status": str((fields.get("status") or {}).get("name") or "")})
        return sorted(out, key=lambda item: (int(re.search(r"(\d+)$", item["key"]).group(1))
                                             if re.search(r"(\d+)$", item["key"]) else 0, item["key"]))

    def repository_settings_payload(project) -> list[dict[str, Any]]:
        configured = {str(item.get("repo") or "").lower(): item for item in project.repositories
                      if isinstance(item, dict) and item.get("repo")}
        names = project.configured_repositories
        return [{"repo": name, "base": str(configured.get(name.lower(), {}).get("base") or ""),
                 "checks": list(configured.get(name.lower(), {}).get("checks") or []),
                 "stack": str(configured.get(name.lower(), {}).get("stack") or ""),
                 "effective": project.settings_for_repo(name)} for name in names]

    @app.get("/v1/projects/{key}/repositories")
    def project_repositories(key: str, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, "project is not configured")
        if not ctx.jira or not github_connection_list(ctx):
            raise HTTPException(503, "Jira and GitHub must be connected to configure repositories")
        try:
            epics = jira_project_epics(key)
            available = github_catalog(user.subject, "project_repositories")
        except Exception as exc:
            log.warning("project repositories could not be loaded (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        by_name = {str(item["full_name"]).lower(): item for item in available}
        assignments = github_connections(ctx).assignments()
        repositories = []
        for row in repository_settings_payload(project):
            catalog_row = by_name.get(row["repo"].lower()) or {}
            reachable = catalog_row.get("connections", [])
            assigned_connection = assignments.get(row["repo"].lower(), catalog_row.get("assigned_connection", ""))
            stale_assignment = bool(assigned_connection and assigned_connection not in reachable)
            selected_connection = catalog_row.get("connection", "")
            if stale_assignment:
                selected_connection = reachable[0] if reachable else ""
            repositories.append({**row, "connection": selected_connection,
                                 "assigned_connection": assigned_connection,
                                 "connection_stale": stale_assignment,
                                 "connections": reachable})
        return {"project": key, "default_repo": project.repo, "connections": connection_summaries(),
                "project_defaults": {"base": project.base, "checks": project.checks, "stack": project.stack},
                "repositories": repositories,
                "epic_repositories": project.epic_repositories or {},
                "epics": epics, "github_repos": available, "stacks": sorted(STACK_TOOLS)}

    @app.put("/v1/projects/{key}/repositories")
    def save_project_repositories(key: str, body: ProjectRepositoriesBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="save_project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            project_audit("save_project_repositories", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        if not ctx.jira or not github_connection_list(ctx):
            project_audit("save_project_repositories", key, user.subject, False,
                          "Jira and GitHub must be connected")
            raise HTTPException(503, "Jira and GitHub must be connected")
        try:
            epics = jira_project_epics(key)
            available_repositories = github_catalog(user.subject, "project_repository_save")
        except Exception as exc:
            project_audit("save_project_repositories", key, user.subject, False, "could not load providers")
            log.warning("project repository save preflight failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        available = {str(item["full_name"]).lower(): item for item in available_repositories}
        gateways = {conn.id: conn.gateway for conn in github_connection_list(ctx)}
        chosen: dict[str, str] = {}  # repository -> connection the owner picked on this save
        epic_keys = {item["key"] for item in epics}
        repos: dict[str, dict[str, Any]] = {}
        error = ""
        for item in body.repositories:
            name = item.repo.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or name.lower() not in available:
                error = f"repository {name} is not visible to any connected GitHub account"
                break
            if item.connection:
                if item.connection not in available[name.lower()]["connections"]:
                    error = f"GitHub connection {item.connection} cannot reach {name}"
                    break
                chosen[name.lower()] = item.connection
            if name.lower() in repos:
                error = f"repository {name} is listed more than once"
                break
            base = item.base.strip()
            effective_base = base or project.base
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", effective_base) or ".." in effective_base:
                error = f"invalid base branch for {name}"
                break
            if item.stack and item.stack not in STACK_TOOLS:
                error = f"unsupported stack for {name}"
                break
            repos[name.lower()] = {"repo": name, "base": base,
                                   "checks": [check.strip() for check in item.checks if check.strip()],
                                   "stack": item.stack.strip()}
        mappings = {str(epic).strip(): str(name).strip() for epic, name in body.epic_repositories.items()
                    if str(name).strip()}
        if not error:
            for epic, name in mappings.items():
                if epic not in epic_keys:
                    error = f"{epic} is not an epic in Jira project {key}"
                    break
                if name.lower() not in available:
                    error = f"repository {name} is not visible to any connected GitHub account"
                    break
                if name.lower() not in repos:
                    repos[name.lower()] = {"repo": name, "base": "", "checks": [], "stack": ""}
        if not error and project.repo.lower() not in repos:
            repos[project.repo.lower()] = {"repo": project.repo, "base": "", "checks": [], "stack": ""}
        if not error:
            for item in repos.values():
                lowered = item["repo"].lower()
                connection = chosen.get(lowered) or (available.get(lowered) or {}).get("connection", "")
                if not connection or connection not in gateways:
                    error = f"no GitHub connection can reach {item['repo']}"
                    break
                branch = item["base"] or project.base
                if not gateways[connection].branch_exists(item["repo"].split("/", 1)[-1], branch):
                    error = f"base branch {branch} is missing from {item['repo']}"
                    break
        if error:
            project_audit("save_project_repositories", key, user.subject, False, error,
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise HTTPException(422, error)

        config_path = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            normalized = sorted(repos.values(), key=lambda item: item["repo"].lower())
            config["repositories"] = normalized
            config["epic_repositories"] = mappings
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = Path(ctx.settings.control_repo).name
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                       f"projects: update repositories for {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project repository update failed (%s)", type(exc).__name__)
            project_audit("save_project_repositories", key, user.subject, False, "control repo commit failed",
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise HTTPException(502, "could not save repository settings to aiflow-control") from exc
        for lowered, connection in chosen.items():
            github_connections(ctx).assign(repos[lowered]["repo"], connection)
            audit("github", "assign_connection", "", "", None, user.subject, True,
                  f"{repos[lowered]['repo']} uses connection {connection}",
                  {"repo": repos[lowered]["repo"], "connection": connection, "project": key})
        refresh_projects()
        project_audit("save_project_repositories", key, user.subject, True,
                      commit or "bot commit created", {"epic_repositories": mappings,
                                                       "repositories": normalized})
        ctx.events.emit("project.repositories_changed", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}",
                        data={"epic_count": len(mappings), "repository_count": len(normalized)})
        return {"saved": True, "project": key, "commit": commit,
                "epic_repositories": mappings, "repositories": normalized}

    @app.post("/v1/projects/preflight")
    def project_preflight_endpoint(body: ProjectPreflightBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_preflight")
        return project_preflight(body, user.subject)

    @app.post("/v1/projects")
    def add_project(body: ProjectCreateBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="add_project",
                            node=body.project[:32])
        with ctx.db.pool.connection() as connection:
            with connection.transaction():
                # Keep the shared admission lock through preflight, the external commit, and repo refresh. A
                # competing request then counts the project just committed before it can take the final slot.
                licensing.lock_resource_admission(connection)
                if not refresh_projects():
                    project_audit("add_project", body.project[:32], user.subject, False,
                                  "control repo refresh failed before capacity check", {"repo": body.repo})
                    raise HTTPException(503, "could not refresh the control repo before checking project capacity")
                require_capacity("projects", len(ctx.repo().projects), connection=connection)
                try:
                    result = project_preflight(body, user.subject)
                except HTTPException as exc:
                    project_audit("add_project", body.project[:32], user.subject, False, str(exc.detail),
                                  {"project_input": body.project, "repo": body.repo})
                    raise
                key = result["project"]
                if key in ctx.repo().projects:
                    project_audit("add_project", key, user.subject, False, "project is already configured",
                                  {"repo": body.repo})
                    raise HTTPException(409, "project is already configured")
                if not result["ok"]:
                    reasons = []
                    if result["missing_statuses"]:
                        reasons.append("missing Jira statuses: " + ", ".join(result["missing_statuses"]))
                    if not result["branch_exists"]:
                        reasons.append(f"base branch {body.base} is missing from {body.repo}")
                    detail = {"message": "; ".join(reasons) or "project checks failed",
                              "missing_statuses": result["missing_statuses"],
                              "branch_exists": result["branch_exists"], "nodes": result["nodes"]}
                    project_audit("add_project", key, user.subject, False, "; ".join(reasons), detail)
                    raise HTTPException(422, detail)

                worker_nodes = {name for name, node in ctx.repo().nodes.items() if node.role == "worker"}
                worker_nodes.update(row["name"] for row in ctx.db.all("SELECT name FROM nodes WHERE role='worker'"))
                checks = [check.strip() for check in body.checks if check.strip()]
                config = {"key": key, "repo": body.repo, "base": body.base, "stack": body.stack,
                          "checks": checks, "merge": {"method": body.merge_method},
                          "limits": {"parallel": body.parallel_limit,
                                     "work_minutes": {"target": 30, "warn": 45, "hard": 60}},
                          "priority": body.priority, "paused": False, "nodes": sorted(worker_nodes)}
                content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
                control_name = Path(ctx.settings.control_repo).name
                try:
                    github = github_control_gateway(ctx)
                    branch = github.default_branch(control_name)
                    commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                                f"projects: add {key} via AI Flow console")
                except HTTPException:
                    raise
                except Exception as exc:
                    log.warning("project add commit failed (%s)", type(exc).__name__)
                    project_audit("add_project", key, user.subject, False, "control repo commit failed", config)
                    raise HTTPException(502, "could not save the project to aiflow-control") from exc
                # Make the committed project visible to the next admission before releasing the lock.
                refresh_projects()
        project_audit("add_project", key, user.subject, True, commit or "bot commit created", config)
        ctx.events.emit("project.added", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}")
        return {"saved": True, "project": key, "commit": commit, "preflight": result}

    @app.post("/v1/projects/{key}/pause")
    def pause_project(key: str, body: ProjectPauseBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="pause_project", node=key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            project_audit("pause_project", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        project_file = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
            config["paused"] = body.paused
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = Path(ctx.settings.control_repo).name
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                            f"projects: {'pause' if body.paused else 'resume'} {key}")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project pause update failed (%s)", type(exc).__name__)
            project_audit("pause_project", key, user.subject, False, "control repo commit failed",
                          {"paused": body.paused})
            raise HTTPException(502, "could not update the project in aiflow-control") from exc
        refresh_projects()
        action = "paused" if body.paused else "resumed"
        project_audit("pause_project", key, user.subject, True, commit or f"{action} by bot commit",
                      {"project": key, "paused": body.paused})
        ctx.events.emit(f"project.{action}", project=key, account=user.subject, outcome=action,
                        reason=f"control repo commit {commit or 'created'}")
        return {"project": key, "status": "paused" if body.paused else "active", "commit": commit}

    @app.delete("/v1/projects/{key}")
    def remove_project(key: str, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="remove_project", node=key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            project_audit("remove_project", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        control_name = Path(ctx.settings.control_repo).name
        try:
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.delete_file(control_name, branch, f"projects/{key}.yaml",
                                            f"projects: remove {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project remove commit failed (%s)", type(exc).__name__)
            project_audit("remove_project", key, user.subject, False, "control repo commit failed")
            raise HTTPException(502, "could not remove the project from aiflow-control") from exc
        refresh_projects()
        project_audit("remove_project", key, user.subject, True, commit or "bot commit created", {"project": key})
        ctx.events.emit("project.removed", project=key, account=user.subject, outcome="removed",
                        reason=f"control repo commit {commit or 'created'}")
        return {"removed": True, "project": key, "commit": commit}

    # --- nodes ----------------------------------------------------------------------------------------------
    @app.get("/v1/install.sh", response_class=PlainTextResponse)
    def node_install_script(request: Request) -> PlainTextResponse:
        script = Path(__file__).resolve().parents[3] / "deploy" / "v4" / "bin" / "aiflow4-bootstrap"
        if not script.is_file():
            raise HTTPException(404, "node bootstrap installer is unavailable")
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
        scheme = (forwarded_proto or request.url.scheme).lower()
        host = forwarded_host or request.headers.get("host", "") or request.url.netloc
        if scheme not in ("http", "https") or not valid_control_host(host):
            raise HTTPException(400, "invalid public control URL")
        base_url = f"{scheme}://{host}"
        return PlainTextResponse(script.read_text(encoding="utf-8").replace("__AIFLOW4_CONTROL_URL__", shlex.quote(base_url)))

    def create_join_grant(name: str, role: str, project: str) -> tuple[str, datetime]:
        """Create the shared five-minute grant used by both Command Center entry points."""
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid node name")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role):
            raise HTTPException(422, "invalid node role")
        repo = ctx.repo()
        if project not in repo.projects:
            raise HTTPException(422, "unknown project")
        dynamic = name not in repo.nodes
        if not dynamic and project not in repo.nodes[name].projects:
            raise HTTPException(422, "project is not assigned to this node in the control repo")
        with ctx.db.pool.connection() as conn:
            with conn.transaction():
                # The count, join grant, and pending node insert share the lock and transaction. A second request
                # waits here, then sees the first request's node before checking the last available slot.
                licensing.lock_resource_admission(conn)
                existing = conn.execute(
                    "SELECT name,removed_at,(token_hash<>'' AND last_heartbeat > now()-interval '75 seconds') AS live"
                    " FROM nodes WHERE name=%s FOR UPDATE", (name,)).fetchone()
                if existing and existing["live"] and not existing["removed_at"]:
                    raise HTTPException(409, "a live node already uses this name")
                if not existing or existing["removed_at"]:
                    count = int(conn.execute(
                        "SELECT count(*) AS count FROM nodes WHERE removed_at IS NULL").fetchone()["count"])
                    require_capacity("machines", count, connection=conn)
                token = secrets.token_urlsafe(32)
                issued = conn.execute("INSERT INTO node_join_tokens (token_hash,node_name,role,project,dynamic,expires_at)"
                                      " VALUES (%s,%s,%s,%s,%s,now()+interval '5 minutes')"
                                      " ON CONFLICT (node_name) WHERE used_at IS NULL DO UPDATE SET"
                                      " token_hash=EXCLUDED.token_hash,role=EXCLUDED.role,project=EXCLUDED.project,"
                                      " dynamic=EXCLUDED.dynamic,created_at=now(),expires_at=EXCLUDED.expires_at"
                                      " RETURNING expires_at",
                                      (h(token), name, role, project, dynamic)).fetchone()
                # Register a pending dynamic node so Command Center can show it before its first heartbeat.
                conn.execute("INSERT INTO nodes (name,role,status,projects,dynamic,accepting_work,removed_at)"
                             " VALUES (%s,%s,'joined',%s,%s,true,NULL) ON CONFLICT (name) DO UPDATE"
                             " SET removed_at=NULL,accepting_work=true WHERE nodes.removed_at IS NOT NULL",
                             (name, role, [project], dynamic))
        return token, issued["expires_at"]

    @app.post("/v1/nodes/join-tokens")
    def issue_join_token(body: JoinTokenBody) -> dict:
        if not ctx.settings.join_token or not secrets.compare_digest(body.provision_key, ctx.settings.join_token):
            raise HTTPException(403, "invalid provisioning key")
        token, expires_at = create_join_grant(body.name, body.role, body.project)
        ctx.events.emit("node.join_token_issued", node=body.name, reason=f"{body.role} · {body.project}")
        return {"token": token, "name": body.name, "role": body.role, "project": body.project,
                "expires_at": expires_at.isoformat()}

    @app.post("/v1/nodes/join-tokens/inspect")
    def inspect_join_token(body: JoinInspectBody) -> dict:
        row = ctx.db.one("SELECT node_name,role,project FROM node_join_tokens"
                         " WHERE token_hash=%s AND used_at IS NULL AND expires_at>now()", (h(body.join_token),))
        if not row:
            raise HTTPException(403, "invalid, expired, or used join token")
        project = ctx.repo().projects.get(row["project"])
        if not project:
            raise HTTPException(409, "project is no longer configured")
        release = current_release()
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", release):
            raise HTTPException(503, "no signed AI Flow release is deployed")
        return {"name": row["node_name"], "role": row["role"], "project": row["project"],
                "stack": project.stack, "stacks": project.stacks, "release": release}

    @app.post("/v1/nodes/join")
    def join(body: JoinBody) -> dict:
        try:
            licensing.require_writable(ctx.db, ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(423, str(exc)) from None
        token = secrets.token_urlsafe(32)
        with ctx.db.pool.connection() as conn:
            row = conn.execute(
                "WITH grant_row AS (UPDATE node_join_tokens SET used_at=now()"
                " WHERE token_hash=%s AND used_at IS NULL AND expires_at>now()"
                " AND (%s::text IS NULL OR node_name=%s)"
                " RETURNING node_name,role,project,dynamic)"
                " INSERT INTO nodes (name,ip,role,token_hash,status,projects,dynamic)"
                " SELECT node_name,%s,role,%s,'joined',ARRAY[project],dynamic FROM grant_row"
                " ON CONFLICT (name) DO UPDATE SET token_hash=EXCLUDED.token_hash,"
                " ip=coalesce(nullif(EXCLUDED.ip,''),nodes.ip), role=EXCLUDED.role,projects=EXCLUDED.projects,"
                " dynamic=EXCLUDED.dynamic,status='joined',accepting_work=true,removed_at=NULL"
                " RETURNING name,role,projects,dynamic",
                (h(body.join_token), body.name, body.name, body.ip, h(token))).fetchone()
            if row:
                audit_in(conn, "node", "join", row["name"], "", None, "node-agent", True, "node joined",
                         {"ip_present": bool(body.ip), "role": row["role"], "project": row["projects"][0]})
        if not row:
            raise HTTPException(403, "invalid, expired, or used join token")
        ctx.events.emit("node.joined", node=row["name"], reason=f"{row['role']} · {row['projects'][0]}")
        repo = ctx.repo()
        project = repo.projects[row["projects"][0]]
        sign_in_requests = ctx.db.all(
            "SELECT id FROM managed_ai_accounts"
            " WHERE node=%s AND removed_at IS NULL AND sign_in_status IN ('queued','starting','waiting')",
            (row["name"],))
        signing_in = {account["id"] for account in sign_in_requests}
        accounts = repo.accounts_on(row["name"])
        return {"node": row["name"], "role": row["role"], "project": row["projects"][0],
                "stack": project.stack, "stacks": project.stacks, "release": current_release(), "node_token": token,
                "accounts": [a.id for a in accounts],
                "worker_accounts": [a.id for a in accounts if not a.paused and a.id not in signing_in]}

    @app.post("/v1/nodes/{name}/heartbeat")
    def node_heartbeat(name: str, body: HeartbeatBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        prev = ctx.db.one("SELECT status,accepting_work FROM nodes WHERE name=%s", (node,))
        ctx.db.run("UPDATE nodes SET status=CASE WHEN accepting_work THEN 'online' ELSE 'draining' END,"
                   " slots_total=%s, slots_used=%s, release_version=%s, tools=%s,"
                   " metrics=%s, release_protocol=%s, last_heartbeat=now(),"
                   " idle_since=CASE WHEN %s=0 THEN coalesce(idle_since,now()) ELSE NULL END WHERE name=%s",
                   (body.slots_total, body.slots_used, body.release_version, J(body.tools),
                    J({**body.metrics, "services": body.services}),
                    body.release_protocol, body.slots_used, node))
        for account_id, expires_at in body.credential_expiries.items():
            ctx.db.run("UPDATE accounts SET credential_expires_at=%s WHERE id=%s AND node=%s",
                       (expires_at, account_id, node))
        from .sa import update_slot_pressure
        update_slot_pressure(ctx)
        heartbeat_status = "online" if not prev or prev.get("accepting_work", True) else "draining"
        ctx.events.ship("node.heartbeat", node=node, outcome=heartbeat_status, slots_total=body.slots_total,
                        slots_used=body.slots_used, release_version=body.release_version, metrics=body.metrics)
        if prev and prev["status"] != "online" and prev.get("accepting_work", True):
            ctx.events.emit("node.online", node=node, reason=f"was {prev['status']}")
            from .incidents import close_incident
            close_incident(ctx, "node_down", node, "heartbeats are back")
        merged: dict[str, dict] = {}
        rank = {"working": 5, "probing": 4, "idle": 3, "unavailable": 2, "error": 1, "stopped": 0}
        for w in body.workers:
            acc = str(w.get("account", ""))
            if not acc:
                continue
            st = str(w.get("state", "unknown")) if w.get("alive", True) else "stopped"
            cur = merged.get(acc)
            if cur is None or rank.get(st, 0) > rank.get(cur["_state"], 0):
                merged[acc] = {**w, "_state": st, "instances": (cur or {}).get("instances", 0) + 1}
            else:
                cur["instances"] = cur.get("instances", 1) + 1
        for acc, w in merged.items():
            state = w.pop("_state")
            # "unavailable" (last probe failed) keeps the status the probe set: cooldown, signed_out or error
            ctx.db.run("UPDATE accounts SET worker_state=%s, status=CASE WHEN status='signed_out' THEN status"
                       " WHEN cooldown_until > now() THEN 'cooldown' WHEN %s = 'unavailable' THEN status"
                       " WHEN status = 'working' AND %s = 'idle' THEN status ELSE %s END,"
                       " updated_at=now() WHERE id=%s AND node=%s", (J(w), state, state, state, acc, node))
            ctx.db.run("UPDATE accounts SET idle_since=CASE WHEN status='idle' THEN coalesce(idle_since,now()) ELSE NULL END,"
                       " cooldown_since=CASE WHEN status='cooldown' THEN coalesce(cooldown_since,now()) ELSE NULL END"
                       " WHERE id=%s AND node=%s", (acc, node))
        repo = ctx.repo()
        sign_in_requests = ctx.db.all(
            "SELECT id,provider,sign_in_request_id,sign_in_status FROM managed_ai_accounts"
            " WHERE node=%s AND removed_at IS NULL AND sign_in_status IN ('queued','starting','waiting')"
            " ORDER BY created_at,id", (node,))
        signing_in = {row["id"] for row in sign_in_requests}
        assigned = [a for a in repo.accounts_on(node) if not a.paused and a.id not in signing_in]
        instances = [a.id if i == 1 else f"{a.id}.{i}" for a in assigned
                     for i in range(1, max(1, int(a.parallel or 1)) + 1)]
        # Legacy agents may execute an installer from an archive before verifying that archive. Keep their
        # automatic release target empty until a trusted signed-release verifier has been bootstrapped manually.
        # Signed-release agents follow the fleet rollout (canary, waves, rollback: docs/v4/fleet-upgrade.md).
        release = fleet_release(node, body.release_version) if body.release_protocol >= 1 else ""
        joined_node = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (node,)) or {}
        node_projects = repo.projects_for_node(node, joined_node.get("projects") or [], joined_node.get("dynamic", False))
        project_stacks = {key: (repo.projects[key].stacks if repo.projects[key].repositories
                                else repo.projects[key].stack)
                          for key in node_projects if key in repo.projects}
        return {"desired": {"accounts": [a.id for a in assigned], "workers": instances,
                            "sign_ins": [{"account": row["id"], "provider": row["provider"],
                                          "request_id": row["sign_in_request_id"], "status": row["sign_in_status"]}
                                         for row in sign_in_requests],
                            "release": release,
                            "projects": node_projects,
                            "project_stacks": project_stacks,
                            "project_services": {key: repo.projects[key].required_services for key in node_projects
                                                 if key in repo.projects and repo.projects[key].required_services},
                            "check_probes": tooling.pending_probes(ctx, node, node_projects),
                            "tools": repo.tools_for_node(node, node_projects), "versions": repo.versions}}

    @app.get("/v1/nodes/{name}/health")
    def node_health(name: str, node: str = Depends(node_auth)) -> dict:
        """What an installer needs to call a new node healthy: a fresh heartbeat and a green project preflight."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        row = ctx.db.one("SELECT status,tools,projects,dynamic,metrics,"
                         " extract(epoch FROM now()-last_heartbeat) AS age FROM nodes WHERE name=%s", (node,)) or {}
        repo = ctx.repo()
        node_projects = repo.projects_for_node(node, row.get("projects") or [], row.get("dynamic", False))
        tools = row.get("tools") or {}
        gaps = missing_tools(tools, repo.tools_for_node(node, node_projects))
        for key in node_projects:
            project = repo.projects.get(key)
            if project and project.stacks:
                gaps += toolchain_gaps(tools, project.tools, repo.versions, project.stacks, require_pins=True)
        gaps = list(dict.fromkeys(gaps))
        # Declared test services (PostgreSQL, Redis) must be healthy too, or the node can claim no work.
        needed_services = list(dict.fromkeys(s for key in node_projects if key in repo.projects
                                             for s in repo.projects[key].required_services))
        reported_services = (row.get("metrics") or {}).get("services")
        unhealthy = service_gaps(reported_services, needed_services)
        age = row.get("age")
        return {"node": node, "status": row.get("status", "unknown"),
                "heartbeat_age_seconds": None if age is None else float(age),
                "projects": node_projects, "missing_tools": gaps, "missing_services": unhealthy,
                "preflight_ok": not gaps and not unhealthy}

    # --- owner-operated node installation and repair ---------------------------------------------------------
    def operation_plan(name: str, body: NodeOperationBody, node_row: dict) -> dict:
        repo = ctx.repo()
        configured = repo.nodes.get(name)
        packages = (configured.packages or []) if configured else []
        services = (configured.services or []) if configured else []
        if not isinstance(packages, list) or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}", p)
                for p in packages):
            raise HTTPException(400, "node packages must be a list of safe package names")
        if not isinstance(services, list) or any(not isinstance(s, dict) for s in services):
            raise HTTPException(400, "node services must be a list of service definitions")
        for service in services:
            unit = service.get("unit")
            if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,180}\.service", unit):
                raise HTTPException(400, "node service unit is invalid")
            healthcheck = service.get("healthcheck", "")
            if not isinstance(healthcheck, str):
                raise HTTPException(400, "service health check must be an HTTP URL")
            if healthcheck:
                parsed = urlsplit(healthcheck)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                        or parsed.password or parsed.fragment):
                    raise HTTPException(400, "service health check must be an HTTP URL without embedded credentials")
        required = repo.tools_for_node(name)
        stacks = [stack for p in (configured.projects if configured else [])
                  if p in repo.projects for stack in repo.projects[p].stacks]
        missing = set(toolchain_gaps(node_row.get("tools") or {}, required, repo.versions, stacks,
                                     require_pins=bool(stacks)))
        selected: list[str]
        if body.action == "repair":
            if body.component:
                raise HTTPException(400, "repair reconciles the full desired state; omit component")
            selected = list(required)
            if packages:
                selected.append("packages")
        else:
            component = body.component.strip()
            if component == "packages":
                if not packages:
                    raise HTTPException(400, "no OS packages are declared for this node")
                selected = [component]
            elif component:
                if component not in required:
                    raise HTTPException(400, "component is not required by this node's control repo state")
                if component not in missing:
                    raise HTTPException(409, "component is already present on this node")
                selected = [component]
            else:
                selected = sorted(missing)
            if not selected:
                raise HTTPException(409, "node has no missing required components")

        # AI CLIs are installed with npm. Include Node.js in the server-authored plan so a clean node
        # receives the prerequisite before any CLI installation; the agent skips it when already healthy.
        if any(tool in {"claude", "codex", "grok", "opencode"} for tool in selected) and "node" not in selected:
            selected.append("node")

        tools = []
        for tool in selected:
            tools.append({"name": tool, "version": pinned_version(repo.versions, tool, stacks),
                          "packages": list(packages) if tool == "packages" else []})
        desired_services = list(services) if body.action == "repair" else []
        return {"action": body.action, "tools": tools, "services": desired_services}

    @app.post("/v1/nodes/{name}/join-command")
    def create_join_command(name: str, body: JoinCommandBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action="join_command", node=name)
        if name != body.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            audit("node", "join_command", name, "", None, user.subject, False, "invalid node name")
            raise HTTPException(400, "invalid node name")
        configured = ctx.repo().nodes.get(name)
        role = body.role or (configured.role if configured else "worker")
        project = body.project or (configured.projects[0] if configured and len(configured.projects) == 1 else "")
        if not project:
            audit("node", "join_command", name, "", None, user.subject, False, "project is required")
            raise HTTPException(422, "select a project for this node")
        try:
            control = validated_join_control_url(ctx.settings.join_control_url)
        except ValueError as exc:
            audit("node", "join_command", name, "", None, user.subject, False, str(exc))
            raise HTTPException(503, "configure AIFLOW4_JOIN_CONTROL_URL to an address reachable from joining nodes") from exc
        version = current_release()
        try:
            release_assets(version)
        except HTTPException as exc:
            audit("node", "join_command", name, "", None, user.subject, False, "no signed release is published")
            raise HTTPException(409, "no signed release is published; run aiflow4-deploy first") from exc
        try:
            token, expires_at = create_join_grant(name, role, project)
        except HTTPException as exc:
            audit("node", "join_command", name, "", None, user.subject, False, str(exc.detail))
            raise
        audit("node", "join_command", name, "", None, user.subject, True, "one-time join command created",
              {"expires_at": expires_at.isoformat(), "role": role, "project": project,
               "ssh_password_saved": False})
        command = join_bootstrap_command(token, control)
        ctx.events.emit("node.join_command.created", node=name, outcome="created",
                        reason="one-time token expires in five minutes", data={"actor": user.subject})
        return {"node": name, "role": role, "project": project, "command": command,
                "expires_at": expires_at.isoformat(), "expires_in_s": 300}

    JOB_ROLES = {"develop": "dev", "verify": "verify", "intake": "intake_refiner", "master": "master"}
    def account_jobs(roles: list[str]) -> list[str]:
        return [job for job, role in JOB_ROLES.items() if role in roles]

    def lock_machine_capacity(connection, name: str) -> None:
        """Serialize all account session-budget checks for this machine across API processes."""
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (f"aiflow4-machine-sessions:{name}",))

    def machine_capacity(repo: ControlRepo, name: str) -> int:
        return sum(max(1, int(account.parallel or 1)) for account in repo.accounts_on(name))

    def save_managed_account(account: Account, roles: list[str], sessions: int, paused: bool,
                             sign_in_url: str = "", sign_in_code: str = "",
                             expires_at: datetime | None = None, connection=None) -> None:
        sql = (
            "INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,explicit_only,paused,"
            "sign_in_url,sign_in_code,sign_in_expires_at,sign_in_started_at,removed_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s<>'' THEN now() ELSE NULL END,NULL)"
            " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,node=EXCLUDED.node,tier=EXCLUDED.tier,"
            "roles=EXCLUDED.roles,parallel=EXCLUDED.parallel,explicit_only=EXCLUDED.explicit_only,paused=EXCLUDED.paused,"
            "sign_in_url=CASE WHEN EXCLUDED.sign_in_url='' THEN managed_ai_accounts.sign_in_url"
            " ELSE EXCLUDED.sign_in_url END,sign_in_code=CASE WHEN EXCLUDED.sign_in_code=''"
            " THEN managed_ai_accounts.sign_in_code ELSE EXCLUDED.sign_in_code END,"
            "sign_in_expires_at=coalesce(EXCLUDED.sign_in_expires_at,managed_ai_accounts.sign_in_expires_at),"
            "sign_in_started_at=CASE WHEN EXCLUDED.sign_in_code<>'' THEN now()"
            " ELSE managed_ai_accounts.sign_in_started_at END,"
            "removed_at=NULL,updated_at=now()")
        params = (account.id, account.provider, account.node, account.tier, roles, sessions, account.explicit_only,
                  paused, sign_in_url, sign_in_code, expires_at, sign_in_code)
        if connection is None:
            ctx.db.run(sql, params)
        else:
            connection.execute(sql, params)

    @app.post("/v1/machines/{name}/accounts")
    def add_managed_account(name: str, body: ManagedAccountCreateBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="add", node=name)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise HTTPException(422, "invalid machine name")
        roles = [JOB_ROLES[job] for job in dict.fromkeys(body.jobs)]
        request_id = str(uuid.uuid4())
        with ctx.db.pool.connection() as connection:
            lock_machine_capacity(connection, name)
            licensing.lock_resource_admission(connection)
            node = connection.execute("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,)).fetchone()
            if not node:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine is not registered", {})
                raise HTTPException(404, "machine is not registered")
            repo = ctx.repo()
            capacity = machine_capacity(repo, name)
            if capacity + body.sessions > 8:
                audit_in(connection, "ai-account", "add", name, "", None, user.subject, False,
                         "machine session limit is eight",
                         {"current_sessions": capacity, "requested_sessions": body.sessions})
                raise HTTPException(422, "a machine can run at most 8 sessions")
            require_capacity("ai_accounts", connection=connection)
            existing_ids = {account.id for account in repo.accounts}
            existing_ids.update(row["id"] for row in connection.execute(
                "SELECT id FROM managed_ai_accounts").fetchall())
            index = 1
            while f"{body.provider}-{name}-{index:02d}" in existing_ids:
                index += 1
            account_id = f"{body.provider}-{name}-{index:02d}"
            connection.execute("INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,paused,"
                               "sign_in_request_id,sign_in_status,sign_in_started_at)"
                               " VALUES (%s,%s,%s,'middle',%s,%s,false,%s,'queued',now())",
                               (account_id, body.provider, name, roles, body.sessions, request_id))
            connection.execute("INSERT INTO accounts (id,provider,node,tier,roles,status)"
                               " VALUES (%s,%s,%s,'middle',%s,'signed_out')",
                               (account_id, body.provider, name, roles))
            audit_in(connection, "ai-account", "add", account_id, "", None, user.subject, True,
                     "account added; sign-in pending", {"node": name, "provider": body.provider,
                                                         "sessions": body.sessions, "jobs": account_jobs(roles)})
        ctx.events.emit("account.added", account=account_id, provider=body.provider, node=name,
                        outcome="sign_in_queued", reason="provider sign-in started on the machine")
        return {"account": account_id, "node": name, "provider": body.provider, "sessions": body.sessions,
                "jobs": account_jobs(roles), "status": "queued", "sign_in_url": "", "code": ""}

    @app.patch("/v1/accounts/{account_id}")
    def update_managed_account(account_id: str, body: ManagedAccountUpdateBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="update", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if body.sessions is None and body.jobs is None and body.action is None:
            raise HTTPException(422, "choose sessions, jobs, pause, or resume")
        with ctx.db.pool.connection() as connection:
            lock_machine_capacity(connection, account.node)
            repo = ctx.repo()
            account = repo.account(account_id)
            if not account:
                raise HTTPException(404, "AI account is not registered")
            sessions = body.sessions if body.sessions is not None else max(1, int(account.parallel or 1))
            total = machine_capacity(repo, account.node)
            if total - max(1, int(account.parallel or 1)) + sessions > 8:
                audit_in(connection, "ai-account", "update", account_id, "", None, user.subject, False,
                         "machine session limit is eight", {"current_sessions": total, "requested_sessions": sessions})
                raise HTTPException(422, "a machine can run at most 8 sessions")
            roles = ([JOB_ROLES[job] for job in dict.fromkeys(body.jobs)] if body.jobs is not None
                     else list(account.roles or []))
            paused = body.action == "pause" if body.action else account.paused
            save_managed_account(account, roles, sessions, paused, connection=connection)
            connection.execute("UPDATE accounts SET roles=%s,updated_at=now() WHERE id=%s", (roles, account_id))
            action = body.action or "update"
            payload = {"sessions": sessions, "jobs": account_jobs(roles), "paused": paused}
            audit_in(connection, "ai-account", action, account_id, "", None, user.subject, True, "updated", payload)
        ctx.events.emit("account.management_changed", account=account_id, node=account.node,
                        outcome=action, reason="account settings updated", data=payload)
        return {"account": account_id, **payload}

    @app.post("/v1/accounts/{account_id}/sign-in")
    def restart_account_sign_in(account_id: str, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_again", node=account_id)
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        if ctx.db.one("SELECT 1 AS active FROM runs WHERE account_id=%s AND status='running' LIMIT 1", (account_id,)):
            raise HTTPException(409, "account has an active session; wait for it to finish before signing in again")
        request_id = str(uuid.uuid4())
        with ctx.db.pool.connection() as connection:
            save_managed_account(account, list(account.roles or []), max(1, int(account.parallel or 1)),
                                 account.paused, connection=connection)
            connection.execute("UPDATE managed_ai_accounts SET sign_in_request_id=%s,sign_in_status='queued',"
                               "sign_in_url='',sign_in_code='',sign_in_expires_at=NULL,sign_in_error='',"
                               "sign_in_started_at=now(),updated_at=now() WHERE id=%s",
                               (request_id, account_id))
            connection.execute("UPDATE accounts SET status='signed_out',last_error='',updated_at=now() WHERE id=%s",
                               (account_id,))
            audit_in(connection, "ai-account", "sign_in_again", account_id, "", None, user.subject, True,
                     "provider sign-in queued on the machine", {"provider": account.provider,
                                                                  "node": account.node})
        ctx.events.emit("account.sign_in_started", account=account_id, provider=account.provider,
                        node=account.node, outcome="queued", reason="owner requested provider sign-in again")
        return {"account": account_id, "status": "queued", "sign_in_url": "", "code": ""}

    @app.get("/v1/accounts/{account_id}/sign-in")
    def account_sign_in_details(account_id: str, request: Request) -> dict:
        console_user(request, {"nodes.manage"})
        row = ctx.db.one("SELECT node,provider,sign_in_status,sign_in_url,sign_in_code,sign_in_error,"
                         "sign_in_expires_at FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL AND sign_in_status<>'idle'", (account_id,))
        if not row:
            raise HTTPException(404, "no active sign-in link for this account")
        return {"account": account_id, "node": row["node"], "provider": row["provider"],
                "status": row["sign_in_status"], "sign_in_url": row["sign_in_url"],
                "code": row["sign_in_code"], "error": row["sign_in_error"],
                "expires_at": row["sign_in_expires_at"].isoformat() if row["sign_in_expires_at"] else None}

    @app.post("/v1/accounts/{account_id}/sign-in/status")
    def report_account_sign_in_status(account_id: str, body: ManagedAccountSignInStatusBody,
                                      node: str = Depends(node_auth)) -> dict:
        row = ctx.db.one("SELECT node,sign_in_request_id,sign_in_status FROM managed_ai_accounts"
                         " WHERE id=%s AND removed_at IS NULL", (account_id,))
        if not row or row["node"] != node:
            raise HTTPException(404, "account sign-in request is not assigned to this machine")
        if row["sign_in_request_id"] != body.request_id:
            raise HTTPException(409, "sign-in request has been replaced")
        if body.sign_in_url:
            try:
                parsed = urlsplit(body.sign_in_url)
                _ = parsed.port
            except ValueError as exc:
                raise HTTPException(422, "provider sign-in URL is invalid") from exc
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(422, "provider sign-in URL must be an HTTPS URL")
        if body.code and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,79}", body.code):
            raise HTTPException(422, "provider sign-in code has an invalid format")
        if body.status == "waiting" and not body.sign_in_url:
            raise HTTPException(422, "provider sign-in must supply its authorization URL")
        with ctx.db.pool.connection() as connection:
            updated = connection.execute(
                "UPDATE managed_ai_accounts SET sign_in_status=%s,"
                "sign_in_url=CASE WHEN %s<>'' THEN %s ELSE sign_in_url END,"
                "sign_in_code=CASE WHEN %s<>'' THEN %s ELSE sign_in_code END,"
                "sign_in_error=%s,updated_at=now() WHERE id=%s AND node=%s AND sign_in_request_id=%s"
                " AND removed_at IS NULL RETURNING id",
                (body.status, body.sign_in_url, body.sign_in_url, body.code, body.code,
                 body.error[:300], account_id, node, body.request_id)).fetchone()
            if not updated:
                raise HTTPException(409, "sign-in request has been replaced")
        return {"account": account_id, "status": body.status}

    @app.post("/v1/accounts/{account_id}/sign-in/complete")
    def complete_account_sign_in(account_id: str, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="sign_in_complete",
                            node=account_id)
        with ctx.db.pool.connection() as connection:
            row = connection.execute(
                "SELECT m.node,m.provider,m.sign_in_status,a.status,a.last_probe_at,m.sign_in_started_at"
                " FROM managed_ai_accounts m JOIN accounts a ON a.id=m.id"
                " WHERE m.id=%s AND m.removed_at IS NULL AND m.sign_in_status<>'idle'"
                " FOR UPDATE OF m", (account_id,)).fetchone()
            if not row:
                raise HTTPException(404, "no pending sign-in for this account")
            if row["sign_in_status"] != "completed":
                raise HTTPException(409, "provider authorization has not completed on the machine")
            if row["status"] not in ("idle", "working") or not row["last_probe_at"] or not row["sign_in_started_at"] \
                    or row["last_probe_at"] < row["sign_in_started_at"]:
                raise HTTPException(409, "the machine has not reported a successful account check since sign-in started")
            connection.execute("UPDATE managed_ai_accounts SET sign_in_url='',sign_in_code='',sign_in_error='',"
                               "sign_in_status='idle',sign_in_request_id='',sign_in_expires_at=NULL,"
                               "sign_in_started_at=NULL,updated_at=now() WHERE id=%s",
                               (account_id,))
            audit_in(connection, "ai-account", "sign_in_complete", account_id, "", None, user.subject, True,
                     "provider account check succeeded", {"node": row["node"], "provider": row["provider"]})
        ctx.events.emit("account.sign_in_completed", account=account_id, provider=row["provider"],
                        node=row["node"], outcome="ready", reason="owner confirmed the successful provider check")
        return {"account": account_id, "signed_in": True}

    @app.delete("/v1/accounts/{account_id}")
    def remove_managed_account(account_id: str, body: ConfirmBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="ai-account", action="remove", node=account_id)
        if not body.confirm:
            audit("ai-account", "remove", account_id, "", None, user.subject, False, "confirmation required")
            raise HTTPException(400, "confirm account removal")
        account = ctx.repo().account(account_id)
        if not account:
            raise HTTPException(404, "AI account is not registered")
        active = ctx.db.one("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'", (account_id,))
        if active and active["n"]:
            raise HTTPException(409, "account has an active session; wait for it to finish before removing it")
        with ctx.db.pool.connection() as connection:
            connection.execute("INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,removed_at)"
                               " VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT (id) DO UPDATE SET removed_at=now(),"
                               "updated_at=now()", (account.id, account.provider, account.node, account.tier,
                                                     account.roles))
            connection.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
            audit_in(connection, "ai-account", "remove", account_id, "", None, user.subject, True,
                     "account removed from machine", {"node": account.node})
        ctx.events.emit("account.removed", account=account_id, provider=account.provider, node=account.node,
                        outcome="removed", reason="removed from Machines and AI screen")
        return {"account": account_id, "removed": True}

    @app.put("/v1/nodes/{name}/work-acceptance")
    def set_node_work_acceptance(name: str, body: ConfirmBody, accepting_work: bool, request: Request) -> dict:
        action = "resume_work" if accepting_work else "stop_taking_work"
        user = console_user(request, {"nodes.manage"}, target="node", action=action, node=name)
        if not accepting_work and not body.confirm:
            audit("node", action, name, "", None, user.subject, False, "confirmation required")
            raise HTTPException(400, "confirm stopping new work on this machine")
        row = ctx.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not row:
            raise HTTPException(404, "machine is not registered")
        ctx.db.run("UPDATE nodes SET accepting_work=%s,status=CASE WHEN last_heartbeat > now()-interval '75 seconds'"
                   " THEN CASE WHEN %s THEN 'online' ELSE 'draining' END ELSE status END WHERE name=%s",
                   (accepting_work, accepting_work, name))
        audit("node", action, name, "", None, user.subject, True,
              "accepting new work" if accepting_work else "stopping new work", {})
        ctx.events.emit("node.work_acceptance_changed", node=name, outcome=action,
                        reason="accepting new work" if accepting_work else "new work disabled")
        return {"node": name, "accepting_work": accepting_work}

    @app.delete("/v1/nodes/{name}")
    def remove_managed_node(name: str, body: ConfirmBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action="remove", node=name)
        if not body.confirm:
            audit("node", "remove", name, "", None, user.subject, False, "confirmation required")
            raise HTTPException(400, "confirm machine removal")
        node = ctx.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,))
        if not node:
            raise HTTPException(404, "machine is not registered")
        active = ctx.db.one("SELECT count(*) AS n FROM runs WHERE node=%s AND status='running'", (name,))
        if active and active["n"]:
            raise HTTPException(409, "machine has active sessions; wait for them to finish before removing it")
        with ctx.db.pool.connection() as connection:
            connection.execute("UPDATE nodes SET removed_at=now(),accepting_work=false,token_hash='',status='offline'"
                               " WHERE name=%s", (name,))
            connection.execute("UPDATE managed_ai_accounts SET removed_at=now(),updated_at=now() WHERE node=%s",
                               (name,))
            connection.execute("DELETE FROM accounts WHERE node=%s", (name,))
            connection.execute("UPDATE node_join_tokens SET used_at=now() WHERE node_name=%s AND used_at IS NULL",
                               (name,))
            audit_in(connection, "node", "remove", name, "", None, user.subject, True, "machine removed", {})
        ctx.events.emit("node.removed", node=name, outcome="removed", reason="removed from Machines and AI screen")
        return {"node": name, "removed": True}

    @app.post("/v1/nodes/{name}/operations", status_code=202)
    def create_node_operation(name: str, body: NodeOperationBody, request: Request) -> dict:
        user = console_user(request, {"nodes.manage"}, target="node", action=body.action, node=name)
        node_row = ctx.db.one("SELECT name, status, token_hash, tools, last_heartbeat"
                              " FROM nodes WHERE name=%s", (name,))
        if not node_row:
            audit("node", body.action, name, "", None, user.subject, False, "node is not registered")
            raise HTTPException(404, "node is not registered")
        heartbeat = node_row["last_heartbeat"]
        if (node_row["status"] != "online" or not node_row["token_hash"] or not heartbeat
                or (utcnow() - heartbeat).total_seconds() > 75):
            audit("node", body.action, name, "", None, user.subject, False, "node agent is not online")
            raise HTTPException(409, "node agent must be online to install or repair")
        try:
            payload = operation_plan(name, body, node_row)
        except HTTPException as exc:
            audit("node", body.action, name, "", None, user.subject, False, str(exc.detail))
            raise
        operation_id = str(uuid.uuid4())
        components = [str(t["name"]) for t in payload["tools"]]
        with ctx.db.pool.connection() as connection:
            connection.execute("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,%s,%s)",
                               (operation_id, name, body.action, user.subject, J(payload)))
            audit_in(connection, "node", body.action, name, operation_id, None, user.subject, True, "queued",
                     {"operation_id": operation_id, "components": components,
                      "service_count": len(payload["services"])})
        ctx.events.emit("node.operation.queued", node=name, outcome="queued",
                        reason=f"{body.action}: {', '.join(components) or 'services'}",
                        data={"operation_id": operation_id, "action": body.action, "actor": user.subject})
        return {"id": operation_id, "node": name, "action": body.action, "status": "queued",
                "components": components}

    @app.get("/v1/nodes/{name}/operations")
    def node_operations(name: str, request: Request) -> dict:
        console_user(request, {"console.read"})
        rows = ctx.db.all("SELECT id, node, action, actor, status, result, created_at, started_at, completed_at"
                          " FROM node_operations WHERE node=%s ORDER BY created_at DESC LIMIT 50", (name,))
        for row in rows:
            row["id"] = str(row["id"])
        return {"node": name, "operations": rows}

    def probe_project(name: str, key: str):
        """The project when this machine serves it (a probe of anything else is refused)."""
        repo = ctx.repo()
        row = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (name,)) or {}
        if key not in repo.projects_for_node(name, row.get("projects") or [], row.get("dynamic", False)):
            raise HTTPException(403, "the node does not serve this project")
        return repo.projects[key]

    @app.post("/v1/nodes/{name}/check-probes/token")
    def check_probe_token(name: str, body: CheckProbeTokenBody, node: str = Depends(node_auth)) -> dict:
        """Read-only token for the clean checkout on which a node runs a project's check commands."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        project = probe_project(name, body.project)
        if body.repo not in project.configured_repositories:
            raise HTTPException(403, "repository does not belong to the project")
        github = github_gateway(ctx, body.repo)
        connection_id = github_connection_used.get()
        if getattr(github, "kind", "app") == "token":
            expires = utcnow() + timedelta(hours=1)
            token = _issue_git_proxy_token(ctx, {
                "purpose": "check_probe", "connection": connection_id, "repo": body.repo.lower(),
                "node": node, "project": body.project, "expires": int(expires.timestamp()),
            })
            audit("github", "check_probe_token", "", "", None, node, True, "read-only relay capability issued",
                  {"repo": body.repo, "project": body.project, "connection": connection_id,
                   "expires": expires.isoformat(), "transport": "control"})
            return {"token": token, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{body.repo}.git"}
        token, expires = github.token(body.repo.split("/", 1)[-1], {"contents": "read", "metadata": "read"})
        audit("github", "check_probe_token", "", "", None, node, True, "issued",
              {"repo": body.repo, "project": body.project, "connection": connection_id,
               "expires": expires.isoformat()})
        return {"token": token, "expires_at": expires.isoformat()}

    @app.post("/v1/nodes/{name}/check-probes")
    def save_check_probe(name: str, body: CheckProbeBody, node: str = Depends(node_auth)) -> dict:
        """A node reports which check commands of a project ran on a clean checkout and which could not."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        project = probe_project(name, body.project)
        if tooling.probe_signature(tooling.probe_items(project)) != body.signature:
            raise HTTPException(409, "the project's check commands changed; run the probe again")
        results = [{k: str(v)[:300] for k, v in item.items() if k in ("repo", "command", "status", "tool", "detail")}
                   for item in body.results]
        tooling.save_probe(ctx, name, body.project, body.signature, results)
        blocked = [r for r in results if r.get("status") == "cannot_run"]
        ctx.events.emit("project.checks_probed", project=body.project, node=node,
                        outcome="cannot_run" if blocked else "ok",
                        reason=("cannot run: " + "; ".join(f"{r.get('command', '')} ({r.get('tool', '')})" for r in blocked))[:300]
                        if blocked else "")
        return {"saved": True, "cannot_run": len(blocked)}

    @app.get("/v1/nodes/{name}/operations/next")
    def next_node_operation(name: str, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        row = ctx.db.one(
            "WITH next AS (SELECT id FROM node_operations WHERE node=%s AND (status='queued' OR"
            " (status='running' AND started_at < now() - interval '30 minutes'))"
            " ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " UPDATE node_operations AS op SET status='running', started_at=now() FROM next"
            " WHERE op.id=next.id RETURNING op.id, op.action, op.payload", (node,))
        if not row:
            return {"operation": None}
        return {"operation": {"id": str(row["id"]), "action": row["action"], "payload": row["payload"]}}

    @app.post("/v1/nodes/{name}/operations/{operation_id}/result")
    def finish_node_operation(name: str, operation_id: str, body: NodeOperationResultBody,
                              node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        try:
            parsed_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise HTTPException(404, "unknown node operation") from exc
        status = body.status
        updated = ctx.db.one(
            "UPDATE node_operations SET status=%s, result=%s, completed_at=now()"
            " WHERE id=%s AND node=%s AND status='running' RETURNING id, action",
            (status, J(body.result), parsed_id, node))
        if not updated:
            raise HTTPException(404, "unknown or completed node operation")
        ctx.events.emit(f"node.operation.{status}", node=node, outcome=status,
                        reason=f"{updated['action']} finished", data={"operation_id": parsed_id,
                                                                     "healthy": body.result.get("healthy")})
        return {"id": parsed_id, "status": status}

    # --- releases: nodes follow a tagged, signed release written by aiflow4-deploy ---------------------------
    def current_release(node: str = "") -> str:
        # During a canary, only the selected node sees the candidate. The file is written by the host updater
        # and shared read-only with control containers; CURRENT remains the fleet-wide stable release.
        canary = rollout.canary_override(RELEASES)
        if node and canary.get("node") == node and canary.get("target"):
            return str(canary["target"])
        return rollout.read_current(RELEASES)

    def fleet_release(node: str, reported: str) -> str:
        """CURRENT moves the fleet through a rollout: the canary and wave members get it, other nodes keep the
        previous release, and a halted rollout sends its members back."""
        canary = rollout.canary_override(RELEASES)
        if canary.get("node") == node and canary.get("target"):
            return str(canary["target"])
        return rollout.desired_release(ctx, node, reported, rollout.read_current(RELEASES))

    def tagged_release(version: str) -> Path:
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
            raise HTTPException(400, "bad release version")
        return RELEASES / version

    def release_assets(version: str) -> tuple[Path, Path, Path, str]:
        directory = tagged_release(version)
        tar_name = f"aiflow4-{version}.tar.gz"
        tarball = directory / tar_name
        bundle = directory / f"{tar_name}.sigstore.json"
        sums = directory / "SHA256SUMS"
        if not all(path.is_file() for path in (tarball, bundle, sums)):
            raise HTTPException(404, "no such signed release")
        checksum = re.compile(rf"^([0-9a-f]{{64}})  {re.escape(tar_name)}$")
        matches = [m.group(1) for line in sums.read_text().splitlines() if (m := checksum.fullmatch(line))]
        if len(matches) != 1:
            raise HTTPException(404, "release checksum manifest is incomplete")
        return tarball, bundle, sums, matches[0]

    @app.get("/v1/releases/{version}/manifest")
    def release_manifest(version: str, _node: str = Depends(node_auth)) -> dict:
        _, _, _, checksum = release_assets(version)
        base = f"/v1/releases/{version}"
        tar_name = f"aiflow4-{version}.tar.gz"
        return {"version": version, "sha256": checksum, "tarball": f"{base}.tar.gz",
                "signature": f"{base}.tar.gz.sigstore.json", "checksums": f"{base}/SHA256SUMS",
                "filename": tar_name}

    @app.get("/v1/releases/{version}/SHA256SUMS")
    def release_checksums(version: str, _node: str = Depends(node_auth)) -> FileResponse:
        _, _, sums, _ = release_assets(version)
        return FileResponse(sums, media_type="text/plain", filename="SHA256SUMS")

    @app.get("/v1/releases/{version}.tar.gz.sigstore.json")
    def release_signature(version: str, _node: str = Depends(release_reader)) -> FileResponse:
        _, bundle, _, _ = release_assets(version)
        return FileResponse(bundle, media_type="application/json", filename=f"aiflow4-{version}.tar.gz.sigstore.json")

    @app.get("/v1/releases/{sha}.tar.gz")
    def release_file(sha: str, node: str = Depends(release_reader)) -> FileResponse:
        if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", sha):
            f, _, _, _ = release_assets(sha)
        elif re.fullmatch(r"[0-9a-f]{7,40}", sha):  # old, already-installed releases remain downloadable
            f = RELEASES / f"aiflow4-{sha}.tar.gz"
        else:
            raise HTTPException(400, "bad release id")
        if not f.is_file():
            raise HTTPException(404, "no such release")
        return FileResponse(f, media_type="application/gzip")

    @app.post("/v1/nodes/{name}/release-failures")
    def release_failure(name: str, body: ReleaseFailureBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", body.release_version):
            raise HTTPException(400, "bad release version")
        detail = body.detail.strip()[:1000] or "release signature or checksum verification failed"
        title = f"node {node} rejected release {body.release_version}"
        from .incidents import open_incident
        opened = open_incident(ctx, "release_verification", f"{node}:{body.release_version}", "critical", title, detail)
        ctx.events.emit("node.release_rejected", node=node, release_version=body.release_version, detail=detail)
        rollout.release_rejected(ctx, node, body.release_version, detail)
        return {"recorded": True, "opened": opened}

    @app.post("/v1/nodes/{name}/worktrees/sweep")
    def worktrees_sweep(name: str, body: WorktreeSweepBody, node: str = Depends(node_auth)) -> dict:
        """Which of a node's worktree directories can go: every directory of a ticket that is Done, and the
        per-run checkouts (intake, impact, triage) of a ticket that has no run in progress."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        remove: list[str] = []
        for entry in body.dirs[:2000]:
            project, _, directory = entry.partition("/")
            m = re.match(r"([A-Z][A-Z0-9]*-\d+)(.*)$", directory)
            if not m or not project or "/" in directory or ".." in entry:
                continue
            key, rest = m.group(1), m.group(2)
            ticket = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))
            running = ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND status='running' LIMIT 1", (key,))
            per_run = bool(re.fullmatch(r"--(intake|impact|triage-.+)", rest))
            if running:
                continue
            if per_run or (ticket and ticket["status"] == "Done"):
                remove.append(entry)
        return {"remove": remove}

    # --- work -----------------------------------------------------------------------------------------------
    def last_dev(key: str, done: bool = False) -> dict | None:
        """The latest finished dev run; done=True: the latest one that delivered work (a failed or blocked run
        in between does not answer the reviewer's findings)."""
        return ctx.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                          + (" AND outcome='done'" if done else "") + " ORDER BY started_at DESC LIMIT 1", (key,))

    def eligible(account_id: str, node: str) -> list[dict]:
        """Tickets this account may take now: verify and triage before new development."""
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if acc.paused or account_master_paused(account_id):
            return []
        if ctx.leases.cooling(account_id):
            return []
        quota_at_risk = quota_forecast_at_risk(account_id)
        node_row = ctx.db.one("SELECT projects,dynamic,tools,metrics,accepting_work FROM nodes WHERE name=%s"
                              " AND removed_at IS NULL", (node,)) or {}
        if not node_row.get("accepting_work", True):
            return []
        projects = repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False))
        projects = [key for key in projects if not repo.projects[key].paused]
        running_by_project = {row["project"]: int(row["count"]) for row in
                              ctx.db.all("SELECT project,count(*) AS count FROM runs"
                                         " WHERE status='running' GROUP BY project")}
        projects = [key for key in projects if not repo.projects[key].parallel_limit or
                    running_by_project.get(key, 0) < repo.projects[key].parallel_limit]
        # A project with a declared stack is offered only after this node reports every pinned tool healthy.
        # Filtering here keeps another project on the same node available while a stack install is pending.
        projects = [p for p in projects if not repo.projects[p].stacks or not toolchain_gaps(
            node_row.get("tools") or {}, repo.projects[p].tools, repo.versions, repo.projects[p].stacks,
            require_pins=True)]
        # Declared test services (PostgreSQL, Redis) must be reported healthy too; other projects stay available.
        reported_services = (node_row.get("metrics") or {}).get("services")
        projects = [p for p in projects
                    if not service_gaps(reported_services, repo.projects[p].required_services)]
        roles = set(acc.roles or ["dev", "verify"])
        # a role switched off for this account (docs/v4/role-health.md) takes only its daily probation ticket
        modes = role_health.modes(ctx.db, acc.id, sorted(roles))

        def allowed(role: str, t: dict) -> bool:
            return modes.get(role) == "on" or (modes.get(role) == "probation" and role_health.probation_ticket(t))

        def probation(role: str) -> bool:
            return modes.get(role) == "probation"

        held = set(ctx.leases.held_keys())
        out: list[dict] = []
        if not projects:
            return []
        prio = {k: int(getattr(p, "priority", 5) or 5) for k, p in repo.projects.items()}
        if "verify" in roles:
            verify_cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "verify"})}
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                                " AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held or t["key"] in verify_cooling:
                    continue
                dev = last_dev(t["key"], done=True)
                if not dev or not dev["pr_url"]:
                    continue
                if dev["account_id"] == acc.id:  # never grade your own work
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                if not allowed("verify", t):
                    continue
                out.append({**t, "step": "verify", "probation": probation("verify")})
        if "master" in roles:
            triage_cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "triage"})}
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
                                " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))",
                                (projects,)):
                if t["key"] in held or t["key"] in triage_cooling:
                    continue
                dev = last_dev(t["key"])
                if dev and dev["account_id"] == acc.id:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                if not allowed("master", t):
                    continue
                out.append({**t, "step": "triage", "probation": probation("master")})
        if "intake_refiner" in roles:
            def backoff(step: str) -> set[str]:
                return {r["ticket_key"] for r in ctx.db.all(INTAKE_BACKOFF_SQL, {"step": step})
                        if r["outcome"] in ("failed", "timeout", "lost") and r["ended_at"]
                        and r["ended_at"] > utcnow() - timedelta(minutes=10)}
            waiting = backoff("intake")
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
                                " AND NOT ('aiflow-hold' = ANY(labels))"
                                " ORDER BY jira_created NULLS LAST, key", (projects,)):
                if t["key"] in held or t["key"] in waiting:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                if not allowed("intake_refiner", t):
                    continue
                out.append({**t, "step": "intake", "probation": probation("intake_refiner")})
            # change requests from the Business view: assess the impact once, while they wait in Backlog
            waiting = backoff("impact")
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
                                " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
                                " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                                " AND r.step = 'impact' AND r.outcome = 'assessed')"
                                " ORDER BY jira_created NULLS LAST, key", (projects,)):
                if t["key"] in held or t["key"] in waiting:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                if not allowed("intake_refiner", t):
                    continue
                out.append({**t, "step": "impact", "probation": probation("intake_refiner")})
        out.sort(key=lambda t: (prio.get(t["project"], 5), t["jira_created"] or utcnow()))
        if "dev" in roles:
            done = {r["key"] for r in ctx.db.all("SELECT key FROM tickets WHERE status = 'Done'")}
            cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "dev"})}
            dev_rows = []
            parked = tooling.parked_tickets(ctx, node_row)  # waiting for a tool this machine lacks
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'"
                                " AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held or t["key"] in cooling or t["key"] in parked or any(b not in done for b in t["blocked_by"]):
                    continue
                labels = set(t["labels"])
                if "tier-senior" in labels and acc.tier != "senior":
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in labels:
                    continue
                if not allowed("dev", t):
                    continue
                dev_rows.append({**t, "step": "dev", "probation": probation("dev")})
            dev_rows.sort(key=lambda t: (prio.get(t["project"], 5), "rework" not in t["labels"],
                                         PRIORITY.get(t["priority"], 2), t["jira_created"] or utcnow()))
            out += dev_rows
        # Keep the normal workflow order stable, but put a generated flow bug first whenever it is eligible.
        out.sort(key=lambda t: "flow-bug" not in (t.get("labels") or []))
        if quota_at_risk and out:
            # Prefer peers only for tickets they can actually claim. A peer on another node, with different
            # project access or roles, must not leave this account's otherwise-routable tickets waiting.
            available_elsewhere: set[tuple[str, str]] = set()
            for peer in quota_peer_accounts(account_id, roles):
                if not rollout.node_compatible(ctx, peer["node"], RELEASES)["compatible"]:
                    continue
                available_elsewhere.update((ticket["key"], ticket["step"])
                                           for ticket in eligible(peer["id"], peer["node"]))
            out = [ticket for ticket in out if (ticket["key"], ticket["step"]) not in available_elsewhere]
        return out

    def master_ready(account_id: str, node: str) -> bool:
        if ctx.leases.cooling(account_id) or account_master_paused(account_id):
            return False
        ready = ctx.db.one("SELECT a.status, n.status AS node_status FROM accounts a JOIN nodes n ON n.name=a.node"
                           " WHERE a.id=%s AND a.node=%s", (account_id, node))
        return bool(ready) and ready["status"] in ("idle", "working", "probing") and ready["node_status"] == "online"

    def master_candidate(account_id: str, node: str) -> dict | None:
        repo = ctx.repo()
        account = repo.account(account_id)
        if not account or account.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if account.paused or "master" not in (account.roles or []) or not master_ready(account_id, node):
            return None
        if account_quota_at_risk(account_id, {"master"}) or (account_id, "master") in role_health.disabled_roles(ctx.db):
            return None
        if not master_budget_state()["enabled"]:
            return None
        held = set(ctx.leases.held_keys())
        if MASTER_SCOPE in held:
            return None
        recent = ctx.db.one("SELECT id, fence, ticket_key, provider, status, outcome, ended_at FROM runs"
                            " WHERE step='master' ORDER BY started_at DESC LIMIT 1")
        failover = False
        if recent:
            lease_lost = (recent["status"] == "running" and
                          ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}")
            failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
            # Prefer the other provider for one cycle after a failure, and only while it has a ready master
            # account. Otherwise any ready master account may take over: cooldown and node checks above
            # already exclude the account that failed, so the flow never waits on an unavailable provider.
            failed_at = recent["ended_at"] or utcnow()
            if (failed_over and recent["provider"] == account.provider
                    and failed_at > utcnow() - timedelta(seconds=MASTER_CYCLE_S)
                    and any(a.provider != account.provider and "master" in (a.roles or [])
                            and master_ready(a.id, a.node) for a in repo.accounts)):
                return None
            # A lost or quota-stopped holder is an account failure, not a failing cycle: no scope backoff.
            failover = failed_over
        backoff = ctx.db.all(BACKOFF_SQL, {"step": "master"})
        if not failover and any(row["ticket_key"] == MASTER_SCOPE for row in backoff):
            return None
        if recent and recent["ended_at"] and recent["ended_at"] > utcnow() - timedelta(seconds=MASTER_CYCLE_S):
            if recent["outcome"] == "done":
                return None
        return {"key": MASTER_SCOPE, "project": "", "status": "Master", "type": "Master", "priority": "Highest",
                "labels": [], "summary": "Master monitoring cycle",
                "description": "Review incidents and follow up on blocked or slow work.",
                "jira_created": None, "step": "master"}

    def entitled_candidates(account: str, node: str) -> tuple[list[dict], str]:
        """Filter offers by account, machine, and project plan limits without affecting active runs."""
        state = licensing.enforce_work_entitlement(ctx, account, node,
                                                    public_key=ctx.settings.license_public_key)
        candidates = eligible(account, node)
        allowed: list[dict] = []
        refused: list[str] = []
        for ticket in candidates:
            try:
                licensing.enforce_project_entitlement(ctx, ticket["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                refused.append(str(exc))
            else:
                allowed.append(ticket)
        reason = refused[0] if candidates and not allowed and refused else ""
        return allowed, reason

    @app.get("/v1/work/candidates")
    def candidates(account: str, node: str = Depends(node_auth)) -> dict:
        try:
            tickets, reason = entitled_candidates(account, node)
        except licensing.LicenseError as exc:
            return {"tickets": [], "reason": str(exc)}
        policy = rollout.node_compatible(ctx, node, RELEASES)
        if not policy["compatible"]:
            return {"tickets": [], "reason": f"version skew: {policy['reason']}"}
        result = {"tickets": [{"key": t["key"], "summary": t["summary"], "priority": t["priority"],
                                "labels": t["labels"], "step": t["step"]} for t in tickets[:20]]}
        if reason:
            result["reason"] = reason
        return result

    @app.get("/v1/master/candidate")
    def master_cycle_candidate(account: str, node: str = Depends(node_auth)) -> dict:
        try:
            licensing.enforce_work_entitlement(ctx, account, node, public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            return {"candidate": None, "reason": str(exc)}
        return {"candidate": master_candidate(account, node)}

    def related_hits(t: dict) -> list[dict]:
        """Up to 3 past entries related to the ticket summary; memory is advice, so an error gives none."""
        try:
            return memory.related(ctx.db, t["project"], t["summary"], exclude_ticket=t["key"], limit=3)
        except Exception as e:  # noqa: BLE001
            log.warning("memory lookup for %s failed: %s", t["key"], e)
            return []

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(node_auth)) -> Any:
        try:
            state = licensing.enforce_work_entitlement(ctx, body.account, node,
                                                        public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
        # a node outside the version skew policy gets no new work (docs/v4/fleet-upgrade.md)
        policy = rollout.node_compatible(ctx, node, RELEASES)
        if not policy["compatible"]:
            return JSONResponse({"claimed": False, "reason": f"version skew: {policy['reason']}"}, status_code=409)
        if body.step == "master":
            candidates = [master_candidate(body.account, node)]
        else:
            candidates = eligible(body.account, node)
            offered = next((candidate for candidate in candidates
                            if candidate["key"] == body.ticket and candidate["step"] == body.step), None)
            if offered:
                try:
                    licensing.enforce_project_entitlement(ctx, offered["project"],
                                                          public_key=ctx.settings.license_public_key, state=state)
                except licensing.LicenseError as exc:
                    return JSONResponse({"claimed": False, "reason": str(exc)}, status_code=409)
        tickets = {(t["key"], t["step"]): t for t in candidates if t}
        t = tickets.get((body.ticket, body.step))
        if not t:
            return JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
        repo = ctx.repo()
        acc = repo.account(body.account)
        triage_context: dict[str, Any] = {}
        if body.step == "triage":
            verify = ctx.db.one("SELECT summary, details FROM runs WHERE ticket_key=%s AND step='verify'"
                                " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            comments = ctx.jira.recent_comments(t["key"], limit=10) if ctx.jira else []
            triage_context = {"last_dev_summary": (last_dev(t["key"]) or {}).get("summary", ""),
                              "verify_findings": ((verify["details"] or {}).get("findings") or verify["summary"])
                              if verify else "", "recent_comments": comments}
            # what the owner already decided, and how the question was classified when it was raised
            triage_context["owner_register"] = [
                {"id": d["id"], "title": d["title"], "decision": d["decision"]} for d in ctx.db.all(
                    "SELECT id,title,decision FROM owner_decisions WHERE kind='decision' AND status='decided'"
                    " AND (project='' OR project=%s) ORDER BY id LIMIT 60", (t["project"],))]
            raised = escalations.open_escalation(ctx.db, t["key"])
            triage_context["escalation_class"] = (raised or {}).get("class", "")
            asked =ctx.db.one("SELECT ts,data FROM events WHERE ticket=%s AND type='review.action'"
                               " AND data->>'action'='ask-ai' ORDER BY ts DESC LIMIT 1", (t["key"],))
            prior_triage = ctx.db.one("SELECT started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                                      " ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if asked and (not prior_triage or asked["ts"] > prior_triage["started_at"]):
                question = str((asked.get("data") or {}).get("text") or "").strip()
                if question:
                    triage_context["last_dev_summary"] = f"Reviewer question: {question}"
            prior_pr = ctx.db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>''"
                                  " ORDER BY started_at DESC LIMIT 1", (t["key"],))
            prior_pr_match = re.search(r"/pull/(\d+)(?:/|$)", (prior_pr or {}).get("pr_url", ""))
            if prior_pr_match:
                triage_context["pr"] = {"url": prior_pr["pr_url"], "number": int(prior_pr_match.group(1))}
        run_id = str(uuid.uuid4())
        probation_id = None
        if t.get("probation"):  # the one easy ticket a day of a switched-off role
            probation_id = role_health.assign_probation(ctx.db, acc.id, role_health.role_of(body.step) or "", t["key"])
            if probation_id is None:
                return JSONResponse({"claimed": False, "reason": "today's probation ticket is already assigned"},
                                    status_code=409)
        c = ctx.leases.claim(t["key"], run_id, ctx.settings.lease_ms)
        if not c.ok:
            if probation_id is not None:
                role_health.drop_probation(ctx.db, probation_id)
            return JSONResponse({"claimed": False, "reason": f"held by {c.holder}"}, status_code=409)
        model, effort = repo.model_for(acc.provider, "senior" if "tier-senior" in t["labels"] else acc.tier)
        run_token = secrets.token_urlsafe(32)
        p = repo.projects.get(t["project"])
        epic = ticket_epic(t)
        ticket_data = {**{k: t[k] for k in ("key", "project", "summary", "description", "labels", "priority", "type")},
                       "parent_key": t.get("parent_key"), "epic": epic}
        prior_dev = None if body.step == "master" else (last_dev(t["key"], done=True) or last_dev(t["key"]))
        selected_repo = p.settings_for_ticket(ticket_data) if p else {}
        # Once development starts, verify and rework keep the repository that produced the PR even if
        # the owner changes the epic mapping while that work waits for review.
        prior_repository = str(((prior_dev or {}).get("details") or {}).get("repository") or "")
        if p and prior_repository and body.step in {"dev", "verify", "qa", "triage"}:
            prior_settings = ((prior_dev or {}).get("details") or {}).get("repository_settings")
            selected_repo = dict(prior_settings) if isinstance(prior_settings, dict) else p.settings_for_repo(prior_repository)
        run_details = ({"repository": selected_repo.get("repo", ""),
                        "repository_settings": selected_repo} if selected_repo else {})
        ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, model, node, fence, token_hash)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (run_id, t["key"], t["project"], body.step, acc.id, acc.provider, model, node, c.fence, h(run_token)))
        if run_details:
            ctx.db.run("UPDATE runs SET details=%s WHERE id=%s", (J(run_details), run_id))
        if body.step == "dev" and prior_dev and prior_dev.get("pr_url") and prior_dev.get("branch"):
            # A rework run carries the PR identity that the previous developer run recorded. The CI-log endpoint
            # uses this row to bind branch and SHA requests to this ticket's pull request.
            ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s, head_sha=%s WHERE id=%s",
                       (prior_dev["pr_url"], prior_dev["branch"], prior_dev.get("head_sha") or "", run_id))
        if probation_id is not None:
            role_health.attach_probation_run(ctx.db, probation_id, run_id)
            ctx.events.emit("role.probation", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                            account=acc.id, outcome="assigned",
                            reason=f"probation ticket for role {role_health.role_of(body.step)}")
        ctx.db.run("UPDATE accounts SET status='working', updated_at=now(), idle_since=NULL, cooldown_since=NULL WHERE id=%s",
                   (acc.id,))
        ctx.events.emit("run.claimed", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                        fence=c.fence, account=acc.id, provider=acc.provider, model=model, node=node)
        extra: dict[str, Any] = {}
        if body.step == "dev":
            tooling.resolve_ticket(ctx, t["key"], node)
            extra["machine_tools"] = tooling.machine_tools(
                ctx.db.one("SELECT tools,metrics FROM nodes WHERE name=%s", (node,)) or {})
        dev = prior_dev
        if body.step == "verify" and dev:
            extra["pr"] = {"url": dev["pr_url"], "number": int(dev["pr_url"].rstrip("/").split("/")[-1]),
                           "branch": dev["branch"], "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
                           "dev_summary": dev["summary"]}
        if body.step == "dev":
            v = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s"
                           " AND step IN ('verify','qa')"
                           " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if v and (not dev or v["started_at"] > dev["started_at"]):
                extra["findings"] = (v["details"] or {}).get("findings") or v["summary"]
            if dev and dev["branch"]:
                extra["branch"] = dev["branch"]
            # the master AI's answer to the question a dev run stopped on, until a dev run finishes or blocks again
            a = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                           " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if a and not ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                                    " AND outcome IN ('done', 'blocked') LIMIT 1", (t["key"], a["started_at"])):
                q = ctx.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                               " AND started_at < %s ORDER BY started_at DESC LIMIT 1", (t["key"], a["started_at"]))
                extra["triage_answer"] = (a["details"] or {}).get("answer") or a["summary"]
                extra["triage_question"] = (q or {}).get("summary") or ""
            # an escalation resolved without a triage run (register, policy message, machine, amendment) is answered
            # the same way; the newer of the two wins
            auto = escalations.pending_answer(ctx.db, t["key"])
            if auto and (not extra.get("triage_answer") or not a or auto["created_at"] > a["started_at"]):
                extra["triage_answer"], extra["triage_question"] = auto["answer"], auto["question"]
            owner_answer = escalations.owner_answered_on_ticket(ctx, t["key"])
            if owner_answer:
                extra["triage_answer"] = owner_answer["answer"]
                extra["triage_question"] = owner_answer["question"]
        if body.step == "triage":
            extra.update(triage_context)
        if body.step == "intake":
            # owners answer intake questions in comments as often as in the description
            try:
                extra["recent_comments"] = ctx.jira.recent_comments(t["key"], limit=10) if ctx.jira else []
            except Exception as e:  # noqa: BLE001
                log.warning("comments of %s for intake failed: %s", t["key"], e)
        if body.step == "impact":
            # the board the assessment compares against: the project's epics and its open work
            extra["project_tickets"] = ctx.db.all(
                "SELECT key, type, status, summary, parent_key FROM tickets WHERE project=%s AND status<>'Done'"
                " AND key<>%s ORDER BY type<>'Epic', key LIMIT 300", (t["project"], t["key"]))
        if body.step == "master":
            extra["master_scope"] = "global"
        else:
            extra["related"] = related_hits(t)
        try:
            ticket_context = f"{t.get('summary', '')}\n{t.get('description', '')}"[:12_000]
            extra["context_pack"] = knowledge.context_pack(ctx.db, project=t.get("project", ""), ticket=t["key"],
                                                           epic=epic or "", query=ticket_context, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - a local search failure cannot prevent a lease
            log.warning("knowledge context pack for %s failed: %s", t["key"], type(exc).__name__)
            extra["context_pack"] = {"project": t.get("project", ""), "ticket": t["key"], "items": []}
        project_data = ({"key": "", "repo": "", "base": "", "hotfix_base": "", "checks": [], "stack": "",
                         "node": node,
                         "work_minutes": {"target": 30, "warn": 45, "hard": 60},
                         "merge": {"method": "squash", "auto": False, "require_human": True}}
                        if body.step == "master" else
                        {"key": p.key, **selected_repo, "hotfix_base": p.hotfix_base,
                         "work_minutes": p.work_minutes(),
                         "merge": {"method": (p.merge or {}).get("method", "squash"),
                                   "auto": bool((p.merge or {}).get("auto", True)),
                                   "require_human": bool((p.merge or {}).get("require_human", False))}}
                        if p else {})
        return {**extra, "claimed": True, "step": body.step, "run_id": run_id, "fence": c.fence, "run_token": run_token,
                "lease_ms": ctx.settings.lease_ms, "model": model, "effort": effort, "provider": acc.provider,
                "account": acc.id, "identity": bot_identity(),
                "ticket": ticket_data,
                "project": project_data}

    # --- configured Jira version releases ------------------------------------------------------------------
    @app.get("/v1/version-releases/candidates")
    def version_release_candidates(account: str, node: str = Depends(node_auth)) -> dict:
        try:
            state = licensing.enforce_work_entitlement(ctx, account, node,
                                                        public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            return {"versions": [], "reason": str(exc)}
        policy = rollout.node_compatible(ctx, node, RELEASES)
        if not policy["compatible"]:
            return {"versions": [], "reason": f"version skew: {policy['reason']}"}
        node_row = ctx.db.one("SELECT accepting_work FROM nodes WHERE name=%s AND removed_at IS NULL", (node,))
        if not node_row or not node_row["accepting_work"]:
            return {"versions": [], "reason": "machine is not accepting new work"}
        if account_master_paused(account):
            return {"versions": [], "reason": "account is paused by master monitoring"}
        if ctx.leases.cooling(account):
            return {"versions": [], "reason": "account quota is cooling or predicted to run out before reset"}
        versions = version_release.candidates(ctx, account, node)
        peer_candidates = quota_peer_release_candidates(account)
        available = [candidate for candidate in versions
                     if (candidate["project"], str(candidate["version_id"]),
                         str(candidate.get("repository") or "")) not in peer_candidates]
        if versions and not available:
            return {"versions": [], "reason": "account quota is cooling or predicted to run out before reset"}
        allowed, refused = [], []
        for candidate in available:
            try:
                licensing.enforce_project_entitlement(ctx, candidate["project"],
                                                      public_key=ctx.settings.license_public_key, state=state)
            except licensing.LicenseError as exc:
                refused.append(str(exc))
            else:
                allowed.append(candidate)
        result = {"versions": allowed}
        if refused and not allowed:
            result["reason"] = refused[0]
        return result

    @app.post("/v1/version-releases/claim")
    def version_release_claim(body: VersionReleaseClaimBody, node: str = Depends(node_auth)) -> Any:
        try:
            state = licensing.enforce_work_entitlement(ctx, body.account, node,
                                                        public_key=ctx.settings.license_public_key)
        except licensing.LicenseError as exc:
            raise HTTPException(409, str(exc)) from None
        policy = rollout.node_compatible(ctx, node, RELEASES)
        if not policy["compatible"]:
            raise HTTPException(409, f"node version skew: {policy['reason']}")
        repo = ctx.repo()
        project = repo.projects.get(body.project)
        acc = repo.account(body.account)
        if account_master_paused(body.account):
            raise HTTPException(409, "account is paused by master monitoring")
        if ctx.leases.cooling(body.account):
            raise HTTPException(409, "account quota is cooling or predicted to run out before reset")
        if (not project or not version_release.enabled(project)
                or project.repo.lower().rstrip("/") == "jooservices/ai-flow"):
            raise HTTPException(403, "project is not enabled for version releases")
        try:
            licensing.enforce_project_entitlement(ctx, project.key,
                                                  public_key=ctx.settings.license_public_key, state=state)
        except licensing.LicenseError as exc:
            raise HTTPException(409, str(exc)) from None
        if not acc or acc.node != node or body.account != acc.id or not ({"master", "release"} & set(acc.roles or [])):
            raise HTTPException(403, "a master or release account assigned to this node is required")
        node_row = ctx.db.one("SELECT projects,dynamic,status,accepting_work FROM nodes WHERE name=%s", (node,)) or {}
        if not node_row.get("accepting_work", False):
            raise HTTPException(409, "machine is not accepting new work")
        if project.key not in repo.projects_for_node(node, node_row.get("projects") or [], node_row.get("dynamic", False)):
            raise HTTPException(403, "project is not assigned to this node")
        version = next((v for v in version_release.project_versions(ctx, project) if v["id"] == body.version_id), None)
        if not version:
            raise HTTPException(404, "configured unreleased Jira version not found")
        requested_repository = body.repository or version.get("repository") or project.repo
        if requested_repository not in (version.get("repositories") or [project.repo]):
            raise HTTPException(422, "repository is not represented in this Jira version")
        version = {**version, "repository": requested_repository}
        project = version_release.project_for_version(ctx, project, version)
        if not version_release.enabled(project) or project.repo.lower().rstrip("/") == "jooservices/ai-flow":
            raise HTTPException(403, "version release is not enabled for the selected repository")
        candidate_key = (project.key, str(version["id"]), project.repo)
        if candidate_key in quota_peer_release_candidates(body.account):
            raise HTTPException(409, "account quota is cooling or predicted to run out before reset")
        state = ctx.db.one("SELECT status FROM version_releases WHERE project=%s AND version_id=%s AND repository=%s",
                           (project.key, version["id"], project.repo))
        if state and state["status"] in ("checking", "released"):
            raise HTTPException(409, "version already has an active or completed release run")
        claim_github = github_gateway(ctx, project.repo)
        if claim_github is None:
            raise HTTPException(503, "GitHub is not connected")
        release_ctx = replace(ctx, github=claim_github)
        snapshot = version_release.post_merge_resume(release_ctx, project, version)
        if snapshot is None:
            snapshot = version_release.evaluate(release_ctx, project, version)
        if not snapshot["ready"]:
            if not snapshot.get("resume_after_merge"):
                version_release.record_gate(ctx, project, version, snapshot)
            raise HTTPException(409, {"reason": "version release gates are not met", "tickets": snapshot["tickets"],
                                      "missing": snapshot["reasons"]})
        key = version_release.version_identity(project.key, version["id"])
        run_id = str(uuid.uuid4())
        lease = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
        if not lease.ok:
            raise HTTPException(409, "version release is already claimed")
        try:
            github = github_gateway(ctx, project.repo)
            if github is None:
                raise HTTPException(503, "GitHub App is not connected")
            connection_id = github_connection_used.get() or DEFAULT_ID
            source_branch = version_release.source_branch(project)
            if snapshot.get("resume_after_merge"):
                source_sha = snapshot["source_sha"]
                release_branch = snapshot["release_branch"]
                branch_created = not github.branch_exists(project.repo_name, release_branch)
                if branch_created:
                    github.create_branch_at_sha(project.repo_name, release_branch, snapshot["release_head_sha"])
            else:
                source_sha = github.branch_sha(project.repo_name, source_branch)
                release_branch = version_release.release_branch(project, version["name"], source_sha)
                branch_created = not github.branch_exists(project.repo_name, release_branch)
                if branch_created:
                    github.create_branch(project.repo_name, release_branch, source_branch)
            tracker = version_release.tracker_for(ctx, project, version["id"], version["name"])
            selected_settings = {"repo": project.repo, "base": project.base,
                                 "checks": version_release.check_commands(project), "stack": project.stack}
            ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,model,node,fence,token_hash,details)"
                       " VALUES (%s,%s,%s,'version_release',%s,%s,'',%s,%s,'',%s)",
                       (run_id, key, project.key, acc.id, acc.provider, node, lease.fence,
                        J({"repository": project.repo, "repository_settings": selected_settings,
                           "github_connection": connection_id})))
            ctx.db.run("INSERT INTO version_releases (project,version_id,version_name,tracker_key,status,gate_detail,ticket_snapshot,repository)"
                       " VALUES (%s,%s,%s,%s,'checking','',%s,%s)"
                       " ON CONFLICT (project,version_id,repository) DO UPDATE SET"
                       " version_name=EXCLUDED.version_name,tracker_key=EXCLUDED.tracker_key,status='checking',"
                       " gate_detail='',ticket_snapshot=EXCLUDED.ticket_snapshot,updated_at=now()",
                       (project.key, version["id"], version["name"], tracker, J(snapshot["tickets"]), project.repo))
            version_release._audit(ctx, project, tracker,
                                   {"id": run_id, "fence": lease.fence, "account_id": acc.id,
                                    "github_connection": connection_id},
                                   "github-release-branch", "created" if branch_created else "existing",
                                   {"branch": release_branch, "source": source_branch, "source_sha": source_sha})
        except Exception:
            ctx.leases.release(key, run_id, lease.fence)
            raise
        ctx.db.run("UPDATE accounts SET status='working',updated_at=now(),idle_since=NULL,cooldown_since=NULL WHERE id=%s",
                   (acc.id,))
        ctx.events.emit("version_release.claimed", ticket=tracker, project=project.key, step="version_release",
                        run_id=run_id, fence=lease.fence, account=acc.id, provider=acc.provider, node=node,
                        outcome="checking", reason=version["name"], data={"version_id": version["id"],
                                                                            "tickets": snapshot["tickets"]})
        project_data = {"key": project.key, "repo": project.repo, "base": project.base,
                        "checks": version_release.check_commands(project), "source_branch": version_release.source_branch(project),
                        "target_branch": version_release.target_branch(project),
                        "release_branch": release_branch, "source_sha": source_sha,
                        "work_minutes": project.work_minutes()}
        ticket = {"key": key, "project": project.key, "summary": f"Release {version['name']}",
                  "description": f"Run full configured project checks on {release_branch}, cut from {source_branch}.",
                  "labels": [], "priority": "Highest", "type": "Release"}
        try:
            context_pack = knowledge.context_pack(ctx.db, project=project.key, ticket=key,
                                                  query=f"{ticket['summary']}\n{ticket['description']}", run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - a local search failure cannot prevent a lease
            log.warning("knowledge context pack for release %s failed: %s", version["name"], type(exc).__name__)
            context_pack = {"project": project.key, "ticket": key, "items": []}
        return {"claimed": True, "step": "version_release", "run_id": run_id, "fence": lease.fence,
                "run_token": secrets.token_urlsafe(32), "lease_ms": ctx.settings.lease_ms,
                "provider": acc.provider, "model": "", "effort": "", "account": acc.id,
                "identity": bot_identity(), "ticket": ticket, "project": project_data, "context_pack": context_pack,
                "version": version, "tracker_key": tracker, "tickets": snapshot["tickets"]}

    @app.post("/v1/version-releases/{version_id}/complete")
    def version_release_complete(version_id: str, body: VersionReleaseCompleteBody,
                                 node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "version_release", "complete",
                     {"version_id": version_id, "head_sha": body.head_sha, "checks": body.checks})
        if run["step"] != "version_release" or run["node"] != node:
            raise HTTPException(403, "run is not a version release owned by this node")
        if run["ticket_key"] != version_release.version_identity(run["project"], version_id):
            raise HTTPException(403, "run holds the lease of another Jira version")
        project = ctx.repo().projects.get(run["project"])
        if not project or not version_release.enabled(project):
            raise HTTPException(403, "project is not enabled for version releases")
        version = next((v for v in version_release.project_versions(ctx, project) if v["id"] == version_id), None)
        if not version:
            raise HTTPException(409, "Jira version is no longer configured and unreleased")
        saved_repository = str(((run.get("details") or {}).get("repository") or ""))
        if saved_repository:
            version = {**version, "repository": saved_repository}
        project = version_release.project_for_version(ctx, project, version)
        if project.repo.lower().rstrip("/") == "jooservices/ai-flow":
            raise HTTPException(403, "AI Flow itself is never released by project version releases")
        commands = version_release.check_commands(project)
        reported = [str(item.get("command") or "") for item in body.checks]
        if not commands or any(command not in reported for command in commands):
            body.error = body.error or "Worker did not report every configured full project check."
        report = {"head_sha": body.head_sha, "source_sha": body.source_sha or body.head_sha,
                  "checks": body.checks, "error": body.error}
        connection_id = str(((run.get("details") or {}).get("github_connection") or ""))
        try:
            if connection_id:
                selected_github = github_gateway_for_connection(ctx, connection_id, project.repo)
            else:
                selected_github = github_gateway(ctx, project.repo)
                connection_id = github_connection_used.get()
        except HTTPException:
            raise
        if selected_github is None:
            raise HTTPException(503, "GitHub is not connected")
        run = dict(run)
        if connection_id:
            run["github_connection"] = connection_id
        release_ctx = replace(ctx, github=selected_github)
        try:
            result = version_release.complete(release_ctx, project, version, run, report)
        except Exception as exc:  # noqa: BLE001 - park the version behind one blocker; idempotent once it is Done
            log.exception("version release publication failed for %s %s", project.key, version["name"])
            return version_release.publish_failed(release_ctx, project, version, run, exc)
        ctx.db.run("UPDATE runs SET head_sha=%s,summary=%s,details=details || %s WHERE id=%s",
                   (body.head_sha, f"version release {result['status']}", J({"release_result": result,
                                                                              "checks": body.checks}), body.run_id))
        return result

    # --- runs -----------------------------------------------------------------------------------------------
    @app.post("/v1/runs/{run_id}/heartbeat")
    def run_heartbeat(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        ok = int(run["fence"]) == body.fence and ctx.leases.renew(run["ticket_key"], run_id, body.fence, ctx.settings.lease_ms)
        if not ok:
            ctx.db.run("UPDATE runs SET status='lost', outcome='lost' WHERE id=%s AND status='running'", (run_id,))
            ctx.events.emit("run.lost", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                            account=run["account_id"], node=node, reason="lease expired or taken")
            raise HTTPException(409, "lease lost")
        ctx.db.run("UPDATE runs SET heartbeat_at=now(), progress=progress || %s WHERE id=%s", (J(body.progress), run_id))
        return {"ok": True, "lease_ms": ctx.settings.lease_ms}

    @app.post("/v1/runs/{run_id}/result")
    def run_result(run_id: str, body: ResultBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        still_owner = ctx.leases.valid(run["ticket_key"], run_id, body.fence)
        details = dict(body.details)
        run_log = details.pop("support_log_text", None)  # raw logs never reach the runs table or its backups
        details.pop("support_log_truncated", None)
        if isinstance(run_log, str) and run_log:
            try:
                support_bundle.store_run_log(ctx, run_id, run_log)
            except Exception as exc:  # noqa: BLE001 - a missing diagnostic log must not lose the run result
                log.warning("support log for run %s not stored: %s", run_id, type(exc).__name__)
        ctx.db.run("UPDATE runs SET outcome=%s, error_kind=%s, reset_at=%s, tokens_in=tokens_in+%s, tokens_out=tokens_out+%s,"
                   " cost_usd=cost_usd+%s, branch=coalesce(nullif(%s,''),branch), pr_url=coalesce(nullif(%s,''),pr_url),"
                   " head_sha=coalesce(nullif(%s,''),head_sha), summary=%s, details=details || %s, status='finished', ended_at=now()"
                   " WHERE id=%s",
                   (body.outcome, body.error_kind, body.reset_at, body.tokens_in, body.tokens_out, body.cost_usd,
                    body.branch, body.pr_url, body.head_sha, body.summary[:2000], J(details), run_id))
        if body.quota:
            ctx.db.run("UPDATE accounts SET quota=%s, updated_at=now() WHERE id=%s", (J(body.quota), run["account_id"]))
        sample_quota = body.quota or (ctx.db.one("SELECT quota FROM accounts WHERE id=%s", (run["account_id"],)) or {}).get("quota") or {}
        ctx.db.run("INSERT INTO account_quota_samples (account_id,provider,model,quota,tokens_in,tokens_out,cost_usd,"
                   "duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (run["account_id"], run["provider"], run["model"], J(sample_quota), body.tokens_in,
                    body.tokens_out, body.cost_usd,
                    int((utcnow() - run["started_at"]).total_seconds() * 1000)))
        if body.error_kind in ("quota", "auth"):
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            ctx.leases.cooldown(run["account_id"], seconds, body.error_kind)
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s, updated_at=now(),"
                       " cooldown_since=CASE WHEN %s='quota' THEN coalesce(cooldown_since,now()) ELSE NULL END,"
                       " idle_since=NULL WHERE id=%s",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300],
                        body.error_kind, run["account_id"]))
        else:
            ctx.db.run("UPDATE accounts SET status='idle', last_error=CASE WHEN %s IN ('failed','timeout') THEN %s ELSE '' END,"
                       " updated_at=now(), idle_since=coalesce(idle_since,now()), cooldown_since=NULL"
                       " WHERE id=%s AND status='working'", (body.outcome, body.summary[:300], run["account_id"]))
        if body.outcome == "tool_missing" and run["step"] == "dev":
            tooling.record_missing(ctx, run, [str(x) for x in body.details.get("missing_tools") or []], body.summary)
        try:
            knowledge.capture_run(ctx.db, run_id, ctx.settings.jira_site)
            knowledge.capture_closed_tickets(ctx.db, ctx.settings.jira_site)
        except Exception as exc:  # noqa: BLE001 - capture cannot undo a completed worker run
            log.warning("knowledge capture for run %s failed: %s", run_id, type(exc).__name__)
        dur = int((utcnow() - run["started_at"]).total_seconds() * 1000)
        if ctx.jira and run["step"] == "dev" and body.outcome not in ("lost",):
            try:  # developer time on the ticket (each dev run, rework included); the detailed timeline is in OpenObserve
                ctx.jira.add_worklog(run["ticket_key"], max(60, dur // 1000), run["started_at"],
                                     f"AI Flow {run['step']} by {run['account_id']} ({run['provider']}"
                                     f"{'/' + run['model'] if run['model'] else ''}) on {node}: {body.outcome}."
                                     f" Run {run_id}, fence {body.fence}.")
            except Exception as e:  # noqa: BLE001
                log.warning("worklog for %s failed: %s", run["ticket_key"], e)
        ctx.events.emit("run.finished", ticket=run["ticket_key"], project=run["project"], step=run["step"], run_id=run_id,
                        fence=body.fence, account=run["account_id"], provider=run["provider"], model=run["model"],
                        node=node, duration_ms=dur, outcome=body.outcome, reason=body.error_kind or body.summary[:200],
                        tokens_in=body.tokens_in, tokens_out=body.tokens_out, cost_usd=body.cost_usd,
                        still_owner=still_owner, pr_url=body.pr_url)
        try:  # quality by role: this result may switch the role off or settle a probation ticket
            role_health.evaluate(ctx, run["account_id"])
            if run["step"] == "verify":  # a verdict also decides the dev run it judged
                dev = last_dev(run["ticket_key"])
                if dev and dev["account_id"] != run["account_id"]:
                    role_health.evaluate(ctx, dev["account_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("role health after run %s failed: %s", run_id, e)
        return {"ok": True, "still_owner": still_owner}

    @app.post("/v1/runs/{run_id}/release")
    def run_release(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        released = ctx.leases.release(run["ticket_key"], run_id, body.fence)
        ctx.db.run("UPDATE runs SET status=CASE WHEN status='running' THEN 'released' ELSE status END,"
                   " ended_at=coalesce(ended_at, now()) WHERE id=%s", (run_id,))
        ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (run["account_id"],))
        ctx.events.emit("run.released", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="released" if released else "not_owner")
        return {"released": released}

    @app.get("/v1/master/context")
    def master_context(run_id: str, fence: int, node: str = Depends(node_auth)) -> dict:
        master_run(run_id, fence, node, "context")
        return master_snapshot()

    @app.post("/v1/master/flow-bugs")
    def master_flow_bugs(body: MasterFlowBugsBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "flow-bug-scan")
        return {"reports": process_flow_bugs(run)}

    @app.post("/v1/master/plan-outcome")
    def master_plan_outcome(body: MasterPlanOutcomeBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "plan-outcome", {"valid": body.valid})
        inserted = ctx.db.one("INSERT INTO master_plan_cycles (run_id, valid) VALUES (%s,%s)"
                              " ON CONFLICT (run_id) DO NOTHING RETURNING run_id",
                              (body.run_id, body.valid))
        recorded = (ctx.db.one("SELECT valid FROM master_plan_cycles WHERE run_id=%s", (body.run_id,))
                    if not inserted else {"valid": body.valid})
        valid = bool(recorded["valid"])
        if inserted:
            ctx.events.emit("master.plan", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                            account=run["account_id"], node=node, outcome="valid" if valid else "invalid",
                            reason="strict-format retry completed" if not valid else "valid action plan")
        streak_row = ctx.db.one(
            "SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid AND recorded_at > "
            "coalesce((SELECT max(recorded_at) FROM master_plan_cycles WHERE valid), 'epoch'::timestamptz)") or {}
        hour_row = ctx.db.one("SELECT count(*) AS n FROM master_plan_cycles WHERE NOT valid"
                              " AND recorded_at >= now()-interval '1 hour'") or {}
        streak, last_hour = int(streak_row.get("n") or 0), int(hour_row.get("n") or 0)
        incident_open = False
        if valid:
            from .incidents import close_incident
            close_incident(ctx, "master_invalid_plan", "global", "master returned a valid action plan")
        elif streak >= 3 or last_hour >= 5:
            from .incidents import open_incident
            incident_open = open_incident(
                ctx, "master_invalid_plan", "global", "critical", "master repeatedly returned an invalid plan",
                f"Invalid plan cycles: {streak} in a row and {last_hour} in the last hour. "
                "The master retried once with a strict JSON reminder on each cycle.")
        return {"valid": valid, "invalid_streak": streak, "invalid_last_hour": last_hour,
                "incident_opened": incident_open}

    def autonomy_report(weeks: int = 8, scope: tuple[str, ...] | None = None) -> dict[str, Any]:
        now = utcnow()
        this_monday = now.date() - timedelta(days=now.weekday())
        first_monday = this_monday - timedelta(days=(weeks - 1) * 7)
        start = datetime.combine(first_monday, datetime.min.time(), tzinfo=timezone.utc)
        end = now
        trend = []
        for index in range(weeks):
            week = first_monday + timedelta(days=index * 7)
            trend.append({"week_start": week.isoformat(), "finished_without_person": 0,
                          "needed_person": 0, "repeated_failures": 0})
        def trend_index(value: datetime) -> int | None:
            date = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
            offset = (date - first_monday).days // 7
            return offset if 0 <= offset < weeks else None

        # A person limited to some projects gets the report of those projects only.
        ticket_filter = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))" if scope is not None else ""
        scope_params: tuple = (list(scope),) if scope is not None else ()
        finished = ctx.db.all(
            "SELECT DISTINCT t.key, t.summary, t.done_at FROM tickets t WHERE t.status='Done'"
            + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " AND t.done_at >= %s AND t.done_at <= %s AND EXISTS (SELECT 1 FROM runs r"
            " WHERE r.ticket_key=t.key AND r.status<>'running' AND lower(r.outcome) IN ('done','pass','passed')"
            " AND r.started_at <= t.done_at AND NOT EXISTS (SELECT 1 FROM runs later"
            " WHERE later.ticket_key=t.key AND later.started_at > r.started_at AND later.started_at <= t.done_at))"
            " AND NOT EXISTS (SELECT 1 FROM write_audit w WHERE w.ticket=t.key AND w.action='transition'"
            " AND w.payload->>'source'='human-review' AND w.result='Done' AND w.ts <= t.done_at)", scope_params + (start, end))
        for item in finished:
            i = trend_index(item["done_at"])
            if i is not None:
                trend[i]["finished_without_person"] += 1
        needed = ctx.db.all("SELECT observation_key, ticket, reason, options, sent_at FROM master_escalations"
                            " WHERE sent_at >= %s AND sent_at <= %s" + ticket_filter + " ORDER BY sent_at DESC",
                            (start, end) + scope_params)
        for item in needed:
            i = trend_index(item["sent_at"])
            if i is not None:
                trend[i]["needed_person"] += 1
        repeated = ctx.db.all("SELECT ts, type FROM events WHERE type IN ('flow_bug.created','flow_bug.repeated')"
                              " AND ts >= %s AND ts <= %s" + ticket_filter, (start, end) + scope_params)
        for item in repeated:
            i = trend_index(item["ts"])
            if i is not None:
                trend[i]["repeated_failures"] += 1
        week_start = datetime.combine(this_monday, datetime.min.time(), tzinfo=timezone.utc)
        current_finished = [row for row in finished if row["done_at"] >= week_start]
        current_needed = [row for row in needed if row["sent_at"] >= week_start]
        current_repeated = ctx.db.all(
            "SELECT f.ticket_key, f.signature, f.repeat_count, f.updated_at, t.summary, t.status"
            " FROM flow_bug_tracker f LEFT JOIN tickets t ON t.key=f.ticket_key"
            " WHERE f.updated_at >= %s" + (" AND t.project=ANY(%s)" if scope is not None else "") +
            " ORDER BY f.updated_at DESC LIMIT 50", (week_start,) + scope_params)
        # Accounts are shared infrastructure, not project data.
        quota_accounts = ctx.db.all("SELECT id, provider, quota FROM accounts ORDER BY id") if scope is None else []
        quota_state = quota_reporting.assessments(ctx.db, quota_accounts, now)
        return {"week_start": this_monday.isoformat(),
                "finished_without_person": current_finished[:50], "needed_person": current_needed[:50],
                "repeated_failures": current_repeated, "trend": trend,
                "escalations": escalations.weekly_metrics(ctx.db, weeks),
                "quota_accounts": [{"id": a["id"], "provider": a["provider"],
                                    "quota_awareness": quota_state.get(a["id"], {})} for a in quota_accounts]}

    @app.get("/v1/master/autonomy")
    def master_autonomy(request: Request) -> dict:
        user = console_user(request, "console.read")
        return {"budget": master_budget_state(), "report": autonomy_report(scope=user.project_keys)}

    @app.put("/v1/master/autonomy/budget")
    def master_budget_update(body: MasterBudgetBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="master", action="daily-budget-update")
        ctx.db.run("INSERT INTO master_settings (singleton,daily_budget_minutes,updated_by) VALUES (true,%s,%s)"
                   " ON CONFLICT (singleton) DO UPDATE SET daily_budget_minutes=EXCLUDED.daily_budget_minutes,"
                   " updated_by=EXCLUDED.updated_by,updated_at=now()",
                   (body.daily_budget_minutes, user.subject))
        audit("master", "daily-budget-update", "", "", None, user.subject, True,
              f"daily budget set to {body.daily_budget_minutes} minutes",
              {"daily_budget_minutes": body.daily_budget_minutes})
        return {"budget": master_budget_state()}

    @app.get("/v1/master/report")
    def master_report(request: Request, weeks: int = Query(8, ge=1, le=52)) -> dict:
        user = console_user(request, "console.read")
        return autonomy_report(weeks, scope=user.project_keys)

    @app.post("/v1/master/actions")
    def master_action(body: MasterActionBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, body.action,
                         {"ticket": body.ticket, "intent_chars": len(body.intent), "action": body.action})
        ticket = body.ticket.strip()
        ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,)) if ticket else None
        policy_project = ticket_row["project"] if ticket_row else None
        policy = ctx.repo().policy_for(policy_project)
        if not body.intent.strip():
            reason = "master action must include the request or observation intent"
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], run_id=body.run_id,
                            fence=body.fence, account=run["account_id"], node=node, outcome="denied", reason=reason,
                            action=body.action)
            raise HTTPException(400, reason)
        # Only the request is matched: what/why/result/detail describe observations (an offline node, a run
        # past its hard limit) and must not turn an allowed follow-up into an owner escalation.
        decision = decide_master_action(policy, body.action, body.intent, body.category)
        ctx.events.emit("policy.decision", ticket=ticket or run["ticket_key"], step="master", run_id=body.run_id,
                        fence=body.fence, account=run["account_id"], node=node, outcome=decision.decision,
                        reason=decision.reason[:200], action=body.action)

        def master_event(outcome: str, reason: str) -> None:
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], project=policy_project or "",
                            step="master", run_id=body.run_id, fence=body.fence, account=run["account_id"],
                            provider=run["provider"], model=run["model"], node=node, outcome=outcome,
                            reason=reason[:300], action=body.action)

        def refuse_master_action(reason: str, status: int = 409) -> None:
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason,
                  {"intent": body.intent[:1000], "why": body.why[:1000]})
            master_event("denied", reason)
            raise HTTPException(status, reason)

        if decision.decision == "escalate":
            options = [str(option).strip()[:500] for option in body.options if str(option).strip()][:5]
            if body.action == "escalate_to_owner" and (not body.why.strip() or not options):
                refuse_master_action("escalate_to_owner requires a reason and available options", 400)
            if not options:
                options = ["Keep the current state and wait for the next health check.",
                           "Choose another healthy account or retry after the reported reset time.",
                           "Ask an owner to investigate and decide the next step."]
            why = (body.why or f"The policy requires a person to decide: {decision.reason}").strip()[:1500]
            observation_key = body.observation_key.strip()[:500]
            if not observation_key:
                if body.kind and body.subject:
                    observation_key = f"incident:{body.kind}:{body.subject}"
                elif ticket:
                    updated = ctx.db.one("SELECT jira_updated FROM tickets WHERE key=%s", (ticket,))
                    observation_key = f"ticket:{ticket}:{updated['jira_updated'].isoformat() if updated and updated['jira_updated'] else 'current'}"
                else:
                    observation_key = f"master:{body.action}:{body.intent[:200]}"
            configured_channels = notify.configured_channels(ctx, "ticket_needs_person")
            created_row = ctx.db.one(
                "INSERT INTO master_escalations"
                " (observation_key,ticket,reason,options,run_id,pending_channels,delivered_channels,intent)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (observation_key) DO NOTHING RETURNING observation_key",
                (observation_key, ticket, why, J(options), body.run_id, configured_channels, [], body.intent[:1500]))
            escalation = ctx.db.one(
                "SELECT reason, options, pending_channels, delivered_channels, intent FROM master_escalations"
                " WHERE observation_key=%s", (observation_key,))
            # A NULL pending list marks rows created before channel delivery tracking was installed. Retry all
            # currently configured channels for those rows; the prior version could not record partial failures.
            pending_channels = set(escalation.get("pending_channels") or [])
            delivered_channels = set(escalation.get("delivered_channels") or [])
            if escalation.get("pending_channels") is None:
                pending_channels.update(set(configured_channels) - delivered_channels)
            elif not pending_channels and not delivered_channels:
                # An observation first seen with no alert channels can be delivered if channels are configured
                # later. Successfully delivered observations have a non-empty delivered list and stay suppressed.
                pending_channels.update(configured_channels)
            else:
                pending_channels.update(set(configured_channels) - delivered_channels)
            pending_before = sorted(pending_channels)
            if pending_before != sorted(escalation.get("pending_channels") or []):
                ctx.db.run("UPDATE master_escalations SET pending_channels=%s WHERE observation_key=%s",
                           (pending_before, observation_key))
            link = (ctx.settings.jira_site.rstrip("/") + f"/browse/{ticket}") if ticket and ctx.settings.jira_site else ""
            why = str(escalation["reason"])
            stored_options = [str(option) for option in (escalation.get("options") or [])]
            message = (f"Why a person is needed: {why}\nRequest: {escalation.get('intent') or body.intent[:1500]}\n"
                       "Available options:\n" + "\n".join(f"- {option}" for option in stored_options))
            attempt_channels = sorted(pending_channels.intersection(configured_channels))
            try:
                sent = (notify.send("warning", f"{ticket or 'AI Flow'} requires an owner decision", message, link,
                                    ctx=ctx, alert_kind="ticket_needs_person", project=policy_project or "AI Flow",
                                    ticket=ticket, needed=why, channels=attempt_channels, queued_ok=True)
                        if attempt_channels else [])
            except Exception as exc:  # Persist the pending channels and retry them on a later master cycle.
                log.warning("master escalation notification failed: %s", type(exc).__name__)
                sent = []
            sent = sorted(set(sent).intersection(attempt_channels))
            pending_channels.difference_update(sent)
            delivered_channels.update(sent)
            ctx.db.run("UPDATE master_escalations SET pending_channels=%s, delivered_channels=%s"
                       " WHERE observation_key=%s",
                       (sorted(pending_channels), sorted(delivered_channels), observation_key))
            already_sent = bool(not created_row and delivered_channels and not pending_channels and not sent)
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"],
                  body.action == "escalate_to_owner", decision.reason,
                  {"intent_chars": len(body.intent), "observation_key": observation_key, "options": options})
            audit("notify", "master-escalation", ticket, body.run_id, body.fence, run["account_id"], True,
                  ",".join(sent) if sent else ("pending: " + ",".join(sorted(pending_channels))
                                                if pending_channels else "no channel configured or duplicate"),
                  {"reason": why, "options": stored_options, "observation_key": observation_key,
                   "pending_channels": sorted(pending_channels)})
            master_event("escalated", why)
            return {"ok": True, "escalated": True, "reason": why, "options": stored_options,
                    "already_sent": already_sent, "sent": sent, "pending_channels": sorted(pending_channels)}

        if decision.decision != "allow":
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False,
                  decision.reason, {"intent_chars": len(body.intent)})
            master_event("denied", decision.reason)
            raise HTTPException(403, decision.reason)

        allowed_ticket_actions = {"retry", "move_to_todo", "restart_run"}
        ticket_row = None
        if body.action in allowed_ticket_actions:
            ticket_row = ctx.db.one("SELECT key, project, status FROM tickets WHERE key=%s", (ticket,)) if ticket else None
            if not ticket_row:
                reason = "a valid Jira ticket is required for this master action"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            if not ctx.jira:
                refuse_master_action("Jira is unavailable", 503)

        result = "ok"
        if body.action in ("retry", "move_to_todo"):
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a ticket in {ticket_row['status']} cannot be retried by the master")
            if body.action == "move_to_todo" and ticket_row["status"] not in ("To Do", "In Progress", "In Verify"):
                refuse_master_action("move_to_todo is limited to tickets in To Do, In Progress or In Verify")
            if ctx.leases.holder(ticket):
                refuse_master_action("ticket still has an active run; use restart_run after its work-time limit")
            if body.action == "retry":
                latest = ctx.db.one("SELECT outcome FROM runs WHERE ticket_key=%s AND status<>'running'"
                                    " ORDER BY started_at DESC LIMIT 1", (ticket,))
                if not latest or latest["outcome"] not in ("failed", "timeout", "lost", "quota", "blocked"):
                    refuse_master_action("retry requires a recent failed, blocked, quota or lost run")
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - record the refusal without exposing Jira internals
                refuse_master_action(f"Jira could not move {ticket} to To Do ({type(exc).__name__})", 502)
            ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket))
            result = f"ticket moved to {status}"
        elif body.action == "pause_account":
            account_id = body.account_id.strip()
            account = ctx.db.one("SELECT id, status FROM accounts WHERE id=%s", (account_id,)) if account_id else None
            if not account:
                refuse_master_action("pause_account requires a configured account_id", 400)
            unhealthy = account["status"] in ("cooldown", "signed_out", "error") or bool(
                ctx.db.one("SELECT 1 AS issue FROM incidents WHERE closed_at IS NULL"
                           " AND kind IN ('account_quota','quota_high') AND (subject=%s OR subject LIKE %s)",
                           (account_id, account_id + ":%")))
            if not unhealthy:
                refuse_master_action("master may pause only an account with a quota or health issue")
            minutes = min(1440, max(1, int(body.pause_minutes)))
            ctx.db.run("INSERT INTO master_account_pauses (account_id,paused_until,reason,run_id)"
                       " VALUES (%s,now()+(%s * interval '1 minute'),%s,%s) ON CONFLICT (account_id) DO UPDATE SET"
                       " paused_until=EXCLUDED.paused_until,reason=EXCLUDED.reason,run_id=EXCLUDED.run_id,updated_at=now()",
                       (account_id, minutes, (body.why or body.intent)[:500], body.run_id))
            result = f"account paused for {minutes} minutes"
            ticket = ""
        elif body.action == "restart_run":
            if not body.target_run_id or not ctx.jira:
                refuse_master_action("restart_run requires target_run_id and an available Jira connection", 400)
            target = ctx.db.one("SELECT * FROM runs WHERE id::text=%s", (body.target_run_id,))
            if not target:
                refuse_master_action("target_run_id does not identify a recorded run", 404)
            if (target["ticket_key"] != ticket or target["step"] == "master" or target["status"] != "running"
                    or not ctx.leases.valid(target["ticket_key"], str(target["id"]), int(target["fence"]))):
                refuse_master_action("target run is not the active fenced run for this ticket")
            if ticket_row["status"] in ("Done", "Needs owner", "Human review"):
                refuse_master_action(f"a run on a ticket in {ticket_row['status']} cannot be restarted by the master")
            project = ctx.repo().projects.get(target["project"])
            hard = project.work_minutes()["hard"] if project else 60
            elapsed = max(0.0, (utcnow() - target["started_at"]).total_seconds() / 60)
            if elapsed < hard:
                refuse_master_action(f"target run has not passed its {hard} minute work-time limit")
            if not ctx.leases.release(target["ticket_key"], str(target["id"]), int(target["fence"])):
                refuse_master_action("target run lease changed before restart")
            ctx.db.run("UPDATE runs SET status='lost',outcome='lost',ended_at=now(),"
                       " details=details || %s WHERE id=%s AND status='running'",
                       (J({"master_restart": {"master_run_id": body.run_id, "reason": body.why or body.intent}}),
                        body.target_run_id))
            ctx.db.run("UPDATE accounts SET status='idle',updated_at=now() WHERE id=%s AND status='working'",
                       (target["account_id"],))
            try:
                status = ctx.jira.transition_to(ticket, "To Do")
            except Exception as exc:  # noqa: BLE001 - the old lease is already fenced off
                reason = f"run {body.target_run_id} was stopped, but Jira could not move {ticket} to To Do ({type(exc).__name__})"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, reason,
                      {"intent": body.intent[:1000], "why": body.why[:1000], "target_run_id": body.target_run_id})
                master_event("partial", reason)
                raise HTTPException(502, reason) from exc
            ctx.db.run("UPDATE tickets SET status=%s,synced_at=now() WHERE key=%s", (status, ticket))
            result = f"stuck run {body.target_run_id} stopped; ticket moved to {status}"
            ticket_row["project"] = target["project"]
        elif body.action == "open_bug":
            candidates = {candidate["signature"] for candidate in flow_bug_candidates(ctx)
                          if candidate["creation_eligible"]}
            if not body.signature or body.signature not in candidates:
                refuse_master_action("open_bug requires an eligible repeated TESTFLOW failure signature")
            reports = process_flow_bugs(run)
            matching = next((report for report in reports if report.get("signature") == body.signature), None)
            if not matching:
                tracked = ctx.db.one("SELECT ticket_key FROM flow_bug_tracker WHERE signature=%s", (body.signature,))
                matching = {"action": "already_reported", "ticket": tracked["ticket_key"]} if tracked else None
            if not matching:
                refuse_master_action("the eligible flow failure could not be recorded; it will be retried next cycle", 502)
            ticket = matching.get("ticket", "")
            result = f"{matching['action']} {ticket}".strip()
        audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, str(result)[:500],
              {"intent": body.intent[:1000], "why": body.why[:1000], "account_id": body.account_id,
               "target_run_id": body.target_run_id, "signature": body.signature})
        master_event("completed", body.why or body.intent or str(result))
        return {"ok": True, "result": result, "escalated": False}

    # --- Jira writes (fenced) ---------------------------------------------------------------------------------
    @app.post("/v1/jira/{key}/transition")
    def jira_transition(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "transition", {"to": body.to})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] == "impact":
            audit("jira", "transition", key, body.run_id, body.fence, run["account_id"], False,
                  "an impact run only comments", {"to": body.to})
            raise HTTPException(403, "an impact run only comments; the owner moves the ticket")
        previous = ctx.db.one("SELECT project, type, status, labels, summary, fields FROM tickets WHERE key=%s", (key,))
        status = ctx.jira.transition_to(key, body.to)
        ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
                   " THEN now() ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, key))
        audit("jira", "transition", key, body.run_id, body.fence, run["account_id"], True, status, {"to": body.to})
        ctx.events.emit("jira.transition", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                        node=node, outcome=status)
        if previous and previous["type"] == "Epic" and previous["status"] != "Done" and status == "Done":
            try:
                project_lifecycle.report_done_epic(ctx, previous["project"], key)
            except Exception as e:  # noqa: BLE001
                log.warning("could not generate completion report for epic %s: %s", key, e)
        if previous and previous["status"] != "Human review" and status == "Human review":
            notify_human_review(ctx, key, {**previous, "key": key})
        return {"status": status}

    @app.post("/v1/jira/{key}/comment")
    def jira_comment(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "comment", {"chars": len(body.body)})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        cid = ctx.jira.comment(key, body.body)
        audit("jira", "comment", key, body.run_id, body.fence, run["account_id"], True, cid, {"chars": len(body.body)})
        return {"comment_id": cid}

    @app.post("/v1/jira/{key}/fields")
    def jira_fields(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "fields", {"fields": list(body.fields), "add": body.add_labels})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] == "impact":
            raise HTTPException(403, "an impact run only comments")
        if body.fields:
            ctx.jira.set_fields(key, body.fields)
        for lab in body.add_labels:
            ctx.jira.add_label(key, lab)
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s = ANY(labels) THEN labels ELSE array_append(labels,%s) END"
                       " WHERE key=%s", (lab, lab, key))
        for lab in body.remove_labels:
            ctx.jira.remove_label(key, lab)
            ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (lab, key))
        audit("jira", "fields", key, body.run_id, body.fence, run["account_id"], True, "ok",
              {"fields": list(body.fields), "add": body.add_labels, "remove": body.remove_labels})
        return {"ok": True}

    # --- escalation classes and the owner decision register (docs/v4/escalations.md) --------------------------
    @app.post("/v1/escalations/raise")
    def escalation_raise(body: EscalationRaiseBody, node: str = Depends(node_auth)) -> dict:
        """A developer run stopped on a question: classify it and resolve the classes that never need a person."""
        run = fenced(body.run_id, body.fence, "escalation", "raise", {"chars": len(body.question)})
        if run["step"] != "dev":
            raise HTTPException(403, "only a developer run raises an escalation here")
        result = escalations.dev_question(ctx, run, body.question)
        audit("escalation", "raise", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              result["class"], {"resolved": result["resolved"], "outcome": result["outcome"]})
        ctx.events.emit("escalation.raised", ticket=run["ticket_key"], project=run["project"], run_id=body.run_id,
                        account=run["account_id"], node=node, outcome=result["class"],
                        reason=("resolved without a person: " if result["resolved"] else "goes to triage: ") + result["reason"])
        return result

    @app.post("/v1/escalations/triage")
    def escalation_triage(body: TriageDecisionBody, node: str = Depends(node_auth)) -> dict:
        """The triage AI's decision goes through the register: applied, amended, asked once, or refused."""
        run = fenced(body.run_id, body.fence, "escalation", "triage", {"decision": body.decision})
        if run["step"] != "triage":
            raise HTTPException(403, "only a triage run decides an escalation")
        if body.decision not in ("ANSWER", "ESCALATE", "AMEND"):
            raise HTTPException(400, "decision must be ANSWER, ESCALATE or AMEND")
        decision = {"decision": body.decision, "answer": body.answer, "why": body.why, "class": body.escalation_class,
                    "options": body.options, "recommended": body.recommended, "topic": body.topic,
                    "keywords": body.keywords, "criterion": body.criterion, "narrowed": body.narrowed}
        result = escalations.triage_decision(ctx, run, decision, body.question)
        audit("escalation", "triage", run["ticket_key"], body.run_id, body.fence, run["account_id"],
              result["action"] != "refused", f"{result['action']}: {result.get('class', '')}",
              {"decision": body.decision, "decision_id": result.get("decision_id")})
        ctx.events.emit("escalation.triaged", ticket=run["ticket_key"], project=run["project"], run_id=body.run_id,
                        account=run["account_id"], node=node, outcome=result["action"], reason=result.get("class", ""))
        return result

    @app.get("/v1/escalations")
    def escalation_overview(request: Request, weeks: int = Query(8, ge=1, le=52)) -> dict:
        console_user(request, "console.read")
        return {"metrics": escalations.weekly_metrics(ctx.db, weeks), "register": escalations.register(ctx.db)}

    @app.post("/v1/decisions")
    def decision_add(body: OwnerDecisionBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="escalations", action="decision-add")
        try:
            row = escalations.add_decision(ctx.db, body.title, body.decision, body.keywords, body.patterns,
                                           body.project, user.subject)
        except re.error as exc:
            raise HTTPException(400, f"a pattern is not a valid regular expression: {exc}") from None
        audit("escalation", "decision-add", "", "", None, user.subject, True, f"D{row['id']} {row['title']}",
              {"decision_id": row["id"]})
        return {"id": row["id"], "register": escalations.register(ctx.db)}

    @app.post("/v1/decisions/{decision_id}/answer")
    def decision_answer(decision_id: int, body: OwnerDecisionAnswerBody, request: Request) -> dict:
        user = console_user(request, "console.write", target="escalations", action="decision-answer")
        try:
            row = escalations.answer_decision(ctx, decision_id, body.choice, body.note, user.subject)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        audit("escalation", "decision-answer", row["ticket_key"], "", None, user.subject, True,
              f"D{row['id']} {row['title']}", {"decision_id": row["id"]})
        return {"id": row["id"], "register": escalations.register(ctx.db)}

    @app.post("/v1/decisions/{decision_id}/retire")
    def decision_retire(decision_id: int, request: Request) -> dict:
        user = console_user(request, "console.write", target="escalations", action="decision-retire")
        row = escalations.retire_decision(ctx.db, decision_id)
        if not row:
            raise HTTPException(404, "unknown decision")
        audit("escalation", "decision-retire", "", "", None, user.subject, True, f"D{row['id']} {row['title']}",
              {"decision_id": row["id"]})
        return {"id": row["id"], "register": escalations.register(ctx.db)}

    @app.get("/v1/github/ci-log")
    def github_ci_log(run_id: str, fence: int, branch: str = "", sha: str = "", lines: int = Query(200, ge=1, le=1000),
                      node: str = Depends(node_auth)) -> dict:
        """The tail of the failing CI job log of the run's pull request: a developer never has to ask for it."""
        run = fenced(run_id, fence, "github", "ci-log", {"branch": branch, "sha": sha[:12], "lines": lines})

        def refuse(reason: str) -> None:
            audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], False, reason,
                  {"branch": branch, "sha": sha[:12]})
            raise HTTPException(403, reason)

        if run["node"] != node:
            refuse("CI logs are available only to the node running this developer run")
        if run["step"] != "dev":
            refuse("CI logs are available only to a developer run")
        project = ctx.repo().projects[run["project"]]
        expected_branch = str(run.get("branch") or "")
        expected_pr = str(run.get("pr_url") or "")
        pr_match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?", expected_pr, re.I)
        expected_repository = repository_for_run(run, project)

        if not pr_match or not expected_branch or not expected_repository:
            refuse("developer run has no recorded pull request")
        pr_repository = f"{pr_match.group(1)}/{pr_match.group(2)}"
        if pr_repository.lower() != expected_repository.lower():
            refuse("recorded pull request does not match the run repository")
        if branch and branch != expected_branch:
            refuse("requested branch is not the run's pull request branch")
        if not branch and not sha:
            raise HTTPException(400, "give the recorded pull request branch or commit sha")
        github = github_gateway(ctx)
        if not github:
            raise HTTPException(503, "GitHub App is not connected")
        repository = expected_repository.split("/", 1)[-1]
        pull_request = github.pr(repository, int(pr_match.group(3)))
        head = pull_request.get("head") or {}
        head_repo = head.get("repo") or {}
        pull_request_sha = str(head.get("sha") or "")
        if (int(pull_request.get("number") or 0) != int(pr_match.group(3))
                or head.get("ref") != expected_branch
                or str(head_repo.get("full_name") or "").lower() != expected_repository.lower()
                or not pull_request_sha):
            refuse("recorded pull request no longer matches the run's repository and branch")
        if sha and sha.lower() != pull_request_sha.lower():
            refuse("requested commit is not the current head of the run's pull request")
        sha = sha or pull_request_sha
        log_tail = github.failing_job_log(repository, sha, lines)
        audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], True,
              log_tail.get("job") or "no failing job", {"sha": sha[:12], "lines": lines})
        return {"sha": sha, **log_tail}

    @app.post("/v1/notify/triage")
    def triage_alert(body: TriageAlertBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "notify", "triage-alert", {"chars": len(body.detail)})
        if run["step"] != "triage":
            raise HTTPException(403, "only a triage run can alert the owner")
        blocks = ctx.db.one("SELECT 1 FROM tickets WHERE status <> 'Done' AND %s::text = ANY(blocked_by) LIMIT 1",
                            (run["ticket_key"],))
        severity = "critical" if blocks else "warning"
        sent = notify.send(severity, f"{run['ticket_key']} needs owner decision", body.detail[:3000],
                           ctx.settings.jira_site.rstrip("/") + f"/browse/{run['ticket_key']}"
                           if ctx.settings.jira_site else "", ctx=ctx, alert_kind="ticket_needs_person",
                           ticket=run["ticket_key"], needed="Review the question and provide an owner decision.")
        audit("notify", "triage-alert", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              ",".join(sent) or "no channel configured", {"severity": severity})
        return {"ok": True, "severity": severity, "sent": sent}

    @app.post("/v1/jira/{key}/description")
    def jira_description(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "description", {"chars": len(body.body)})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] != "intake":
            raise HTTPException(403, "only an intake run replaces the description")
        if not body.body.strip():
            raise HTTPException(400, "the new description is empty")
        ctx.jira.set_description(key, body.body)
        ctx.db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (body.body, key))
        audit("jira", "description", key, body.run_id, body.fence, run["account_id"], True, "replaced",
              {"chars": len(body.body)})
        ctx.events.emit("jira.description", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                        node=node, outcome="replaced")
        return {"ok": True}

    # --- GitHub (fenced) --------------------------------------------------------------------------------------
    @app.post("/v1/github/token")
    def github_token(body: GitHubTokenBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        permissions = ({"contents": "read", "metadata": "read"}
                       if run["step"] in ("triage", "version_release") else
                       {"contents": "write", "metadata": "read", "workflows": "write"})
        if run["step"] == "version_release" and not version_release.enabled(project):
            raise HTTPException(403, "project is not enabled for version releases")
        github = github_gateway(ctx, repository)
        if getattr(github, "kind", "app") == "token":
            branch = body.branch.strip()
            if branch and (run["step"] != "dev" or not branch.startswith(f"{run['ticket_key']}-")
                           or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                           or branch.startswith("/") or branch.endswith("/") or ".." in branch
                           or "@{" in branch):
                raise HTTPException(422, "the Git transfer branch is invalid for this run")
            expires = utcnow() + timedelta(hours=1)
            connection_id = github_connection_used.get()
            capability = _issue_git_proxy_token(ctx, {
                "connection": connection_id, "repo": repository.lower(), "run_id": body.run_id,
                "fence": body.fence, "node": node, "branch": branch,
                "expires": int(expires.timestamp()),
            })
            audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
                  "repository Git relay capability issued",
                  {"repo": repository, "branch": branch, "expires": expires.isoformat(), "transport": "control"})
            return {"token": capability, "expires_at": expires.isoformat(),
                    "transport_url": f"{ctx.settings.control_url.rstrip('/')}/v1/github/git/{repository}.git"}
        token, expires = github.token(repository.split("/", 1)[-1], permissions)
        audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "issued",
              {"repo": repository, "expires": expires.isoformat()})
        return {"token": token, "expires_at": expires.isoformat()}

    async def github_git_proxy(git_path: str, request: Request) -> Response:
        """Broker token-backed Git smart HTTP without exposing the stored access token to a worker."""
        capability = _verify_git_proxy_token(ctx, _git_proxy_credential(request))
        if not capability:
            raise HTTPException(401, "Git repository transfer credential is invalid or expired",
                                headers={"WWW-Authenticate": 'Basic realm="AI Flow Git transfer"'})
        repository, separator, suffix = git_path.partition(".git")
        if (not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or repository.lower() != str(capability.get("repo", ""))):
            raise HTTPException(403, "Git repository transfer is outside this credential's scope")
        suffix = ".git" + suffix
        if request.method == "GET" and suffix == ".git/info/refs":
            service = request.query_params.get("service", "")
            if service not in ("git-upload-pack", "git-receive-pack"):
                raise HTTPException(400, "unsupported Git service")
            operation = service
        elif request.method == "POST" and suffix in (".git/git-upload-pack", ".git/git-receive-pack"):
            operation = suffix.rsplit("/", 1)[-1]
        else:
            raise HTTPException(404, "unsupported Git repository transfer operation")

        purpose = str(capability.get("purpose") or "run")
        run = None
        run_id = ""
        fence = -1
        if purpose == "check_probe":
            if operation != "git-upload-pack":
                raise HTTPException(403, "check-probe Git credentials are read-only")
            node_name = str(capability.get("node") or "")
            project_key = str(capability.get("project") or "")
            project = probe_project(node_name, project_key)
            if repository.lower() not in {name.lower() for name in project.configured_repositories}:
                raise HTTPException(409, "check-probe repository is no longer configured")
        elif purpose == "run":
            run_id = str(capability.get("run_id") or "")
            run = run_row(run_id)
            fence = int(capability.get("fence", -1))
            if (run["node"] != capability.get("node") or int(run["fence"]) != fence
                    or not ctx.leases.valid(run["ticket_key"], run_id, fence)
                    or not matches_run_repository(repository_for_run(run, ctx.repo().projects.get(run["project"])),
                                                  repository)):
                raise HTTPException(409, "Git repository transfer run is no longer active")
            if operation == "git-receive-pack" and not capability.get("branch"):
                raise HTTPException(403, "Git push is not allowed by this read-only credential")
        else:
            raise HTTPException(403, "Git repository transfer credential has an unsupported purpose")
        body_bytes = await request.body() if request.method == "POST" else b""
        branch = str(capability.get("branch") or "")
        if request.method == "POST" and operation == "git-receive-pack":
            targets = _receive_pack_target(body_bytes)
            if targets != {f"refs/heads/{branch}"}:
                raise HTTPException(403, "Git push is limited to the branch assigned to this run")

        connection_id = str(capability.get("connection") or "")
        gateway = github_gateway_for_connection(ctx, connection_id, repository)
        if getattr(gateway, "kind", "app") != "token":
            raise HTTPException(403, "Git relay credentials are only valid for access-token connections")
        upstream_token, _ = gateway.token(repository.split("/", 1)[-1])
        forwarded_headers = {key: value for key, value in request.headers.items()
                             if key.lower() in {"accept", "content-type", "git-protocol"}}
        try:
            import anyio
            upstream = await anyio.to_thread.run_sync(
                _github_git_upstream, repository, suffix, request.method,
                list(request.query_params.multi_items()), forwarded_headers, body_bytes, upstream_token)
        except httpx.HTTPError as exc:
            log.warning("GitHub repository transfer failed (%s)", type(exc).__name__)
            raise HTTPException(502, "GitHub repository transfer failed") from exc
        if request.method == "POST" and operation == "git-receive-pack":
            if run is None:
                raise HTTPException(403, "check-probe Git credentials are read-only")
            accepted = 200 <= upstream.status_code < 300
            audit("github", "git-push", run["ticket_key"], run_id, fence, run["account_id"], accepted,
                  "pushed" if accepted else f"GitHub returned {upstream.status_code}",
                  {"repo": repository, "branch": branch})
        response_headers = {key: value for key, value in upstream.headers.items()
                            if key.lower() in {"content-type", "cache-control", "expires", "pragma"}}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

    @app.get("/v1/github/git/{git_path:path}", operation_id="githubGitProxyGet")
    async def github_git_proxy_get(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.post("/v1/github/git/{git_path:path}", operation_id="githubGitProxyPost")
    async def github_git_proxy_post(git_path: str, request: Request) -> Response:
        return await github_git_proxy(git_path, request)

    @app.post("/v1/github/pr")
    def github_pr(body: PRBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "pr", {"repo": body.repo, "head": body.head})
        project = ctx.repo().projects.get(run["project"])
        repository = repository_for_run(run, project)
        if not project or not matches_run_repository(repository, body.repo):
            raise HTTPException(403, "repository does not belong to the run's project")
        github = github_gateway(ctx, repository)
        settings = settings_for_run(run, project)
        if body.base != settings["base"]:
            raise HTTPException(403, "pull request base does not match the repository settings")
        pr = github.create_pr(repository.split("/", 1)[-1], body.head, body.base, body.title, body.body, body.draft)
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s WHERE id=%s", (pr["html_url"], body.head, body.run_id))
        audit("github", "pr", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, pr["html_url"],
              {"number": pr.get("number"), "draft": body.draft, "repo": repository})
        ctx.events.emit("github.pr", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="opened", pr_url=pr["html_url"])
        return {"url": pr["html_url"], "number": pr.get("number")}

    @app.post("/v1/github/merge")
    def github_merge(body: MergeBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "merge", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run can merge")
        return merge_command(run, body.number, body.title, body.method, node)

    def review_data(project_filter: str = "", project_scope: tuple[str, ...] | None = None) -> dict:
        repo = ctx.repo()
        configured = sorted(repo.projects)
        if project_scope is not None:
            configured = [key for key in configured if key in project_scope]
        if project_filter and project_filter not in configured:
            raise HTTPException(400, "unknown project filter")
        if project_filter:
            where, params = " WHERE project=%s", (project_filter,)
            project_where, project_params = " AND project=%s", (project_filter,)
            ticket_filter, ticket_params = " AND ticket IN (SELECT key FROM tickets WHERE project=%s)", (project_filter,)
        elif project_scope is not None:
            where, params = " WHERE project=ANY(%s)", (list(project_scope),)
            project_where, project_params = " AND project=ANY(%s)", (list(project_scope),)
            ticket_filter, ticket_params = " AND ticket IN (SELECT key FROM tickets WHERE project=ANY(%s))", (list(project_scope),)
        else:
            where, params = "", ()
            project_where, project_params = "", ()
            ticket_filter, ticket_params = "", ()
        metrics_tickets = ctx.db.all("SELECT key, project, status, labels, blocked_by, done_at, jira_updated, synced_at"
                                     " FROM tickets" + where, params)
        events = ctx.db.all("SELECT ts,type,ticket,project,outcome,reason,data FROM events"
                            " WHERE type IN ('jira.transition','review.action')" + ticket_filter + " ORDER BY ts",
                            ticket_params)
        runs = ctx.db.all("SELECT ticket_key,project,step,account_id,outcome,summary,details,started_at,ended_at FROM runs"
                          " WHERE 1=1" + project_where + " ORDER BY started_at", project_params)
        audits = ctx.db.all("SELECT ts,ticket,action,target,accepted,result,payload FROM write_audit"
                            " WHERE (target='human-review' OR (target='jira' AND action='transition'))"
                            + ticket_filter + " ORDER BY ts", ticket_params)
        metrics = review_metrics.calculate_review_metrics(metrics_tickets, events, runs, audits)
        return {"metrics": metrics, "projects": configured}

    @app.get("/v1/reviews")
    def human_reviews(request: Request, project: str = Query(default="")) -> dict:
        """Queue waiting items with their action set and project-filtered human-review metrics."""
        user = (console_user(request, "view.human_review") if ctx.settings.console_login
                else optional_console_user(request))
        repo = ctx.repo()
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            audit("console", "access_denied", "", "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "project": project,
                                                    "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        metrics_data = review_data(project, scope)
        if project:
            where, params = " AND project=%s", (project,)
        elif scope is not None:
            where, params = " AND project=ANY(%s)", (list(scope),)
        else:
            where, params = "", ()
        tickets = ctx.db.all("SELECT key, project, type, summary, description, labels, parent_key, status, blocked_by"
                             " FROM tickets WHERE (status IN ('Human review','Needs owner') OR"
                             " (cardinality(blocked_by)>0 AND status NOT IN ('Done','Closed')))" + where
                             + " ORDER BY project, key", params)
        all_statuses = {row["key"]: row["status"] for row in ctx.db.all("SELECT key,status FROM tickets")}
        epics = {r["key"]: r["status"] for r in ctx.db.all(
            "SELECT key, status FROM tickets WHERE key='TESTFLOW-5' AND type='Epic'")}
        out = []
        for ticket in tickets:
            if ticket["status"] not in ("Human review", "Needs owner") and not any(
                    str(blocker) not in all_statuses or all_statuses[str(blocker)] not in ("Done", "Closed")
                    for blocker in (ticket.get("blocked_by") or [])):
                continue
            dev = ctx.db.one("SELECT pr_url, summary, details FROM runs WHERE ticket_key=%s AND pr_url<>''"
                             " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
            latest = ctx.db.one("SELECT step,outcome,summary,details,started_at FROM runs"
                                " WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
            pr_url = (dev or {}).get("pr_url", "")
            match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
            project_config = repo.projects.get(ticket["project"])
            repository = repository_for_run(dev or {}, project_config) if project_config else ""
            pr_detail: dict[str, Any] = {}
            if project_config and match:
                try:
                    pr_detail = github_gateway(ctx, repository).pr(
                        repository.split("/", 1)[-1], int(match.group(1))) or {}
                except Exception as e:  # noqa: BLE001
                    log.warning("could not load PR details for %s: %s", ticket["key"], e)
            verify = ctx.db.one("SELECT outcome, summary, details, started_at FROM runs"
                                " WHERE ticket_key=%s AND step='verify' ORDER BY started_at DESC LIMIT 1",
                                (ticket["key"],))
            details = (verify or {}).get("details") or {}
            preview_url = ""
            preview = (project_config.environments or {}).get("preview") if project_config else None
            if isinstance(preview, dict):
                preview_url = str(preview.get("url") or "")
            elif isinstance(preview, str):
                preview_url = preview
            if ticket["project"] != "TESTFLOW" or epics.get("TESTFLOW-5") != "Done":
                preview_url = ""
            open_blocker = any(str(blocker) not in all_statuses or
                               all_statuses[str(blocker)] not in ("Done", "Closed")
                               for blocker in (ticket.get("blocked_by") or []))
            kind = review_item_kind({**ticket, "_has_open_blocker": open_blocker}, latest)
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
                "thread": review_thread(ticket["key"]),
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
        threads = []
        for key in sorted(thread_dates, key=lambda k: thread_dates[k], reverse=True)[:50]:
            ticket = ctx.db.one("SELECT key,project,summary,status FROM tickets WHERE key=%s", (key,))
            thread = review_thread(key)
            if ticket and thread:
                threads.append({**ticket, "thread": thread})
        return {"tickets": out, "threads": threads, **metrics_data}

    @app.get("/v1/reviews/export.csv")
    def export_review_metrics(request: Request, project: str = Query(default="")) -> PlainTextResponse:
        user = (console_user(request, "view.human_review") if ctx.settings.console_login
                else optional_console_user(request))
        scope = user.project_keys if user and user.project_keys is not None else None
        if project and scope is not None and project not in scope:
            audit("console", "access_denied", "", "", None, user.subject, False,
                  "not available for your role", {"path": request.url.path, "project": project,
                                                    "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")
        metrics = review_data(project, scope)["metrics"]
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("metric", "value", "project"))
        for name, value in (
            ("people_needed_this_week", metrics["people_needed_this_week"]),
            ("people_needed_eight_weeks_ago", metrics["people_needed_eight_weeks_ago"]),
            ("finished_with_no_person_pct", metrics["finished_with_no_person_pct"]),
            ("median_wait_minutes", metrics["median_wait_minutes"]),
            ("approved_first_time_pct", metrics["approved_first_time_pct"]),
            ("sent_back_or_rejected_pct", metrics["sent_back_or_rejected_pct"]),
        ):
            writer.writerow((name, "" if value is None else value, project or "all"))
        writer.writerow(())
        writer.writerow(("week", "reason", "count", "project"))
        for week in metrics["weekly"]:
            for reason, count in week["by_reason"].items():
                writer.writerow((week["week"], reason, count, project or "all"))
        return PlainTextResponse(output.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=human-review-metrics.csv"})

    @app.post("/v1/reviews/{key}/approve")
    def approve_review(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to approve")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = merge_command(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                note = f"Human review approval could not merge PR #{number}: {result.get('message', 'merge refused')}"
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review approved in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            status = reviewer_transition(key, run_id, fence, "Done", "human review approved")
            record_reviewer_action(ticket, run_id, fence, "approve")
            finish_reviewer_run(key, run_id, fence, "merged", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review approval failed for %s", key)
            audit("human-review", "approve", key, run_id, fence, "reviewer-console", False,
                  "review approval failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise HTTPException(502, "could not complete the review approval") from e

    @app.post("/v1/reviews/{key}/rework")
    def request_review_rework(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        comment = body.comment.strip()
        if not comment:
            raise HTTPException(400, "a rework comment is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            note = f"Human review requested rework:\n\n{comment}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, add=("rework",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human review requested rework")
            record_reviewer_action(ticket, run_id, fence, "rework", comment)
            finish_reviewer_run(key, run_id, fence, "rework", comment)
            return {"status": status}
        except HTTPException:
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rework failed for %s", key)
            audit("human-review", "rework", key, run_id, fence, "reviewer-console", False,
                  "review rework failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise HTTPException(502, "could not return the ticket for rework") from e

    @app.post("/v1/reviews/{key}/reject")
    def reject_review(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(400, "a rejection reason is required")
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if not number:
                raise HTTPException(409, "ticket has no pull request to reject")
            note = f"Human review rejected this change.\n\nReason: {reason}"
            reviewer_comment(key, run_id, fence, number, note)
            run = fenced(run_id, fence, "github", "close", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            repository = repository_for_run(run, project)
            gateway = github_gateway(ctx, repository)
            closed = gateway.close_pr(repository.split("/", 1)[-1], number)
            audit("github", "close", key, run_id, fence, run["account_id"], True,
                  "pull request closed", {"number": number, "repo": repository})
            reviewer_labels(key, run_id, fence, add=("rejected",), remove=("require-human",))
            status = reviewer_transition(key, run_id, fence, "Done", "human review rejected")
            record_reviewer_action(ticket, run_id, fence, "reject", reason)
            finish_reviewer_run(key, run_id, fence, "rejected", reason)
            return {"status": status, "closed": bool(closed)}
        except HTTPException:
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rejection failed for %s", key)
            audit("human-review", "reject", key, run_id, fence, "reviewer-console", False,
                  "review rejection failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review rejection failed")
            raise HTTPException(502, "could not reject the reviewed change") from e

    @app.post("/v1/reviews/{key}/ask-ai")
    def ask_review_ai(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        question = (body.question or body.comment).strip()
        if not question:
            raise HTTPException(400, "a question for the AI is required")
        ticket, run_id, fence, number = reviewer_run(key, ("Human review", "Needs owner", "To Do", "In Progress", "In Verify"))
        try:
            note = f"Human review asked the AI:\n\n{question}"
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = ticket["status"]
            if status != "Needs owner":
                status = reviewer_transition(key, run_id, fence, "Needs owner", "reviewer asked the AI")
            record_reviewer_action(ticket, run_id, fence, "ask-ai", question)
            finish_reviewer_run(key, run_id, fence, "question", question)
            return {"status": status, "thread": review_thread(key)}
        except HTTPException:
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review AI question failed for %s", key)
            audit("human-review", "ask-ai", key, run_id, fence, "reviewer-console", False,
                  "AI question failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "AI question failed")
            raise HTTPException(502, "could not send the question to the AI") from e

    @app.post("/v1/reviews/{key}/answer")
    def answer_review_question(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        answer = (body.answer or body.comment).strip()
        if not answer:
            raise HTTPException(400, "an answer is required")
        ticket, run_id, fence, number = reviewer_run(
            key, ("Needs owner",), allowed_kinds=("question_from_ai", "decision_needed"))
        try:
            reviewer_comment(key, run_id, fence, number, f"Human answer to the AI:\n\n{answer}")
            reviewer_labels(key, run_id, fence, remove=("owner-escalated",))
            status = reviewer_transition(key, run_id, fence, "To Do", "human answered the AI question")
            record_reviewer_action(ticket, run_id, fence, "answer", answer)
            finish_reviewer_run(key, run_id, fence, "answered", answer)
            return {"status": status}
        except HTTPException:
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review answer failed for %s", key)
            audit("human-review", "answer", key, run_id, fence, "reviewer-console", False,
                  "human answer failed", {"kind": review_item_kind(ticket)})
            finish_reviewer_run(key, run_id, fence, "failed", "human answer failed")
            raise HTTPException(502, "could not record the answer") from e

    @app.post("/v1/reviews/{key}/skip-gate")
    def skip_review_gate(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            if "require-human" not in (ticket.get("labels") or []):
                raise HTTPException(409, "this ticket has no human review gate to skip")
            if not number:
                raise HTTPException(409, "ticket has no pull request to skip the gate for")
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = merge_command(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                note = f"Human review gate could not be skipped because PR #{number} did not merge: "
                note += result.get("message", "merge refused")
                reviewer_comment(key, run_id, fence, number, note)
                audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                      result.get("message", "merge refused"), {"kind": "review_before_merge"})
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            note = f"Human review gate skipped in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            reviewer_comment(key, run_id, fence, number, note)
            reviewer_labels(key, run_id, fence, remove=("require-human",))
            status = reviewer_transition(key, run_id, fence, "Done", "human review gate skipped")
            record_reviewer_action(ticket, run_id, fence, "skip-gate")
            finish_reviewer_run(key, run_id, fence, "skipped_gate", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review gate skip failed for %s", key)
            audit("human-review", "skip-gate", key, run_id, fence, "reviewer-console", False,
                  "review gate skip failed", {"kind": "review_before_merge"})
            finish_reviewer_run(key, run_id, fence, "failed", "review gate skip failed")
            raise HTTPException(502, "could not skip the human review gate") from e

    @app.post("/v1/github/update-branch")
    def github_update_branch(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "update-branch", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run updates a PR branch")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        res = github.update_branch(repository.split("/", 1)[-1], body.number)
        audit("github", "update-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], res["ok"],
              res["message"][:200], {"number": body.number, "repo": repository})
        return res

    @app.get("/v1/github/checks")
    def github_checks(run_id: str, fence: int, sha: str, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node or not ctx.leases.valid(run["ticket_key"], run_id, fence):
            raise HTTPException(409, "not the owner of this run")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        return {"checks": github.checks(repository.split("/", 1)[-1], sha)}

    @app.post("/v1/github/comment")
    def github_comment(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "comment", {"number": body.number})
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        github.comment(repository.split("/", 1)[-1], body.number, body.body)
        audit("github", "comment", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "ok",
              {"number": body.number, "repo": repository})
        return {"ok": True}

    @app.post("/v1/github/delete-branch")
    def github_delete_branch(body: BranchCleanupBody, node: str = Depends(node_auth)) -> dict:
        """Delete the ticket's remote work branch through the connection assigned to its repository."""
        run = fenced(body.run_id, body.fence, "github", "delete-branch", {"branch": body.branch})
        if run["step"] not in ("dev", "verify"):
            raise HTTPException(403, "only a development or verify run cleans up a branch")
        branch = body.branch.strip()
        if (not branch.startswith(f"{run['ticket_key']}-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", branch)
                or branch.endswith("/") or ".." in branch):
            raise HTTPException(422, "the branch does not belong to this ticket")
        project = ctx.repo().projects[run["project"]]
        repository = repository_for_run(run, project)
        github = github_gateway(ctx, repository)
        try:
            deleted = github.delete_branch(repository.split("/", 1)[-1], branch)
        except GitHubError as exc:
            audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], False,
                  "GitHub refused the branch deletion", {"branch": branch, "repo": repository})
            raise HTTPException(502, "GitHub refused the branch deletion") from exc
        audit("github", "delete-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              "deleted" if deleted else "already gone", {"branch": branch, "repo": repository})
        return {"ok": True, "deleted": deleted}

    # --- policy hook ------------------------------------------------------------------------------------------
    @app.post("/v1/policy/decide")
    async def policy_decide(request: Request, authorization: str = Header(default="")) -> dict:
        payload = await request.json()
        token = authorization.removeprefix("Bearer ").strip()
        run = ctx.db.one("SELECT * FROM runs WHERE token_hash=%s AND status='running'", (h(token),)) if token else None
        tool = str(payload.get("tool_name", ""))
        if not run:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "no active AI Flow run for this token"}}
        if run["step"] == "master":
            d = Decision("deny", "master runs return an action plan; all effects go through the fenced master API")
        elif run["step"] == "triage":
            d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                                 str(payload.get("cwd") or ""),
                                 "a triage run reads the base branch and cannot edit files")
        elif run["step"] == "verify" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            d = Decision("deny", "a verify run reviews and tests; it does not edit code")
        elif run["step"] == "intake":
            d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                                 str(payload.get("cwd") or ""), "an intake run rewrites the ticket text; it does not change files")
        elif run["step"] == "impact":
            d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                                 str(payload.get("cwd") or ""), "an impact run assesses a change request; it does not change files")
        else:
            d = decide(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                       str(payload.get("cwd") or ""))
        d = explain(d, tool, payload.get("tool_input") or {})  # every refusal names the allowed alternative
        ctx.events.emit("policy.decision", ticket=run["ticket_key"], run_id=str(run["id"]), account=run["account_id"],
                        node=run["node"], outcome=d.decision, reason=d.reason[:200], tool=tool,
                        command=str((payload.get("tool_input") or {}).get("command", ""))[:300])
        return d.hook_output()

    # --- events from nodes ------------------------------------------------------------------------------------
    # --- account probes: a tiny prompt tells whether an account can work and how much quota is left -------------
    @app.get("/v1/accounts/{account_id}/spec")
    def account_spec(account_id: str, node: str = Depends(node_auth)) -> dict:
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        model, effort = repo.model_for(acc.provider, acc.tier)
        return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}

    @app.post("/v1/accounts/{account_id}/probe")
    def account_probe(account_id: str, body: ProbeBody, node: str = Depends(node_auth)) -> dict:
        acc = ctx.repo().account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if body.quota:
            ctx.db.run("UPDATE accounts SET quota=%s WHERE id=%s", (J(body.quota), account_id))
        ctx.db.run("INSERT INTO account_quota_samples (account_id,provider,model,quota,tokens_in,tokens_out,cost_usd,"
                   "duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                   (account_id, acc.provider, body.model, J(body.quota), body.tokens_in, body.tokens_out,
                    body.cost_usd, body.duration_ms))
        ctx.db.run("UPDATE accounts SET last_probe_at=now(), updated_at=now() WHERE id=%s", (account_id,))
        if "credential_expires_at" in body.model_fields_set:
            ctx.db.run("UPDATE accounts SET credential_expires_at=%s WHERE id=%s",
                       (body.credential_expires_at, account_id))
        if body.error_kind in ("quota", "auth"):
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            ctx.leases.cooldown(account_id, seconds, body.error_kind)
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s,"
                       " cooldown_since=CASE WHEN %s='quota' THEN coalesce(cooldown_since,now()) ELSE NULL END,"
                       " idle_since=NULL WHERE id=%s AND status<>'working'",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300],
                        body.error_kind, account_id))
        elif body.ok:
            ctx.leases.uncool(account_id)
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'idle' END,"
                       " cooldown_until=NULL, last_error='', cooldown_since=NULL,"
                       " idle_since=CASE WHEN status='working' THEN NULL ELSE coalesce(idle_since,now()) END"
                       " WHERE id=%s", (account_id,))
        else:
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'error' END, last_error=%s,"
                       " cooldown_since=NULL, idle_since=NULL"
                       " WHERE id=%s", (body.summary[:300] or body.error_kind, account_id))
        ctx.events.emit("account.probe", account=account_id, provider=acc.provider, node=node,
                        outcome="ok" if body.ok else (body.error_kind or "error"), reason=body.summary[:200],
                        duration_ms=body.duration_ms, tokens_in=body.tokens_in, tokens_out=body.tokens_out,
                        cost_usd=body.cost_usd)
        return {"ok": True}

    @app.post("/v1/events")
    def events_in(body: EventsBody, node: str = Depends(node_auth)) -> dict:
        n = 0
        for e in body.events[:500]:
            e = dict(e)
            t = str(e.pop("type", "node.event"))
            e["node"] = node
            ctx.events.emit(t, **e)
            n += 1
        return {"accepted": n}

    # --- state for dashboards ---------------------------------------------------------------------------------
    @app.get("/v1/projects/{key}/versions")
    def project_versions(key: str) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        versions = ctx.db.all(
            "SELECT v.id, v.name, v.released, v.release_date, count(t.key)::int AS ticket_count,"
            " count(t.key) FILTER (WHERE t.status='Done')::int AS done_count"
            " FROM versions v LEFT JOIN ticket_versions tv ON tv.version_id=v.id"
            " LEFT JOIN tickets t ON t.key=tv.ticket_key WHERE v.project=%s"
            " GROUP BY v.id, v.name, v.released, v.release_date ORDER BY v.name, v.id", (key,))
        for version in versions:
            version["open_tickets"] = ctx.db.all(
                "SELECT t.key, t.summary, t.status FROM ticket_versions tv"
                " JOIN tickets t ON t.key=tv.ticket_key"
                " WHERE tv.version_id=%s AND t.project=%s AND t.status <> 'Done' ORDER BY t.key",
                (version["id"], key))
        return {"project": key, "versions": versions}

    @app.get("/v1/projects/{key}/report")
    def project_report(key: str, epic: str | None = None, format: str = Query(default="json")):
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        if format not in ("json", "md", "csv", "html"):
            raise HTTPException(400, "format must be json, md, csv or html")
        try:
            report = build_report(ctx, key, epic)
        except ReportNotFound as e:
            raise HTTPException(404, str(e)) from e
        if format == "md":
            filename = f"{epic or key}-progress.md"
            return PlainTextResponse(render_markdown(report), media_type="text/markdown",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "csv":
            filename = f"{epic or key}-progress.csv"
            return PlainTextResponse(render_csv(report), media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "html":
            return HTMLResponse(render_html(report))
        return report

    @app.post("/v1/projects/{key}/close")
    def close_project(key: str, body: CloseProjectBody | None = None) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        result = project_lifecycle.close_project(ctx, key, body.actor if body else "owner")
        return {"project": key, "status": result["status"], "version": result["version"],
                "report": result.get("report"), "markdown": result.get("markdown"),
                "study_case_ref": result["study_case_ref"], "created": result["created"],
                "sent_channels": result["sent_channels"]}

    @app.get("/v1/projects/{key}/study-cases")
    def project_study_cases(key: str, limit: int = Query(50, ge=1, le=100)) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        cases = project_lifecycle.study_cases(ctx.db, key, limit)
        return {"project": key, "study_cases": cases}

    # --- Business view: change requests (same access as the console, no node token) -----------------------------
    def business_project(key: str) -> str:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        return key

    @app.get("/v1/business/{key}/change-requests")
    def business_change_requests(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "jira_site": ctx.settings.jira_site, "epics": business.epics(ctx.db, project),
                "change_requests": business.change_requests(ctx.db, project, ctx.settings.jira_site)}

    @app.get("/v1/business/{key}/release-readiness")
    def business_release_readiness(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_readiness(ctx.db, project)}

    @app.get("/v1/business/{key}/release-progress")
    def business_release_progress(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "versions": business.release_progress_of(ctx.db, project)}

    @app.post("/v1/business/{key}/change-requests")
    def business_change_request(key: str, body: ChangeRequestBody) -> dict:
        project = business_project(key)
        try:
            title, description, epic = business.validate(ctx.db, project, body.title, body.description, body.epic)
        except business.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        created = business.create(ctx, project, title, description, epic)
        audit("jira", "create", created["key"], "", None, business.ACTOR, True, created["status"],
              {"project": project, "type": "Story", "labels": [business.LABEL], "epic": epic,
               "chars": len(description)})
        ctx.events.emit("business.change_request", ticket=created["key"], project=project, outcome=created["status"],
                        reason=title[:200])
        return created

    # --- Stakeholder acceptance and UAT feedback --------------------------------------------------------------
    def stakeholder_project(key: str):
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, f"unknown project {key}")
        return project

    def stakeholder_changes(project: str) -> list[dict[str, Any]]:
        rows = ctx.db.all(
            "SELECT DISTINCT ON (t.key) t.key, t.summary, t.type, t.status, t.parent_key, p.type AS parent_type,"
            " r.branch, r.head_sha, r.summary AS change_summary, r.details, r.started_at, r.ended_at"
            " FROM runs r JOIN tickets t ON t.key=r.ticket_key LEFT JOIN tickets p ON p.key=t.parent_key"
            " WHERE r.project=%s AND t.project=%s AND r.step='dev' AND r.status<>'running'"
            " AND r.outcome IN ('done','merged')"
            " ORDER BY t.key, r.started_at DESC", (project, project))
        rows.sort(key=lambda r: r["ended_at"] or r["started_at"], reverse=True)
        out = []
        for row in rows[:30]:
            details = row["details"] or {}
            epic = row["key"] if row["type"] == "Epic" else (
                row["parent_key"] if row["parent_type"] == "Epic" else "")
            release_commit = (details.get("release") or details.get("version") or row["head_sha"] or row["branch"] or "")
            out.append({"key": row["key"], "summary": row["summary"], "type": row["type"],
                        "status": row["status"], "epic": epic, "branch": row["branch"],
                        "commit": row["head_sha"], "release_commit": release_commit,
                        "change_summary": row["change_summary"],
                        "changed_at": (row["ended_at"] or row["started_at"]).isoformat()})
        return out

    @app.get("/v1/stakeholder/{key}")
    def stakeholder_view(key: str) -> dict:
        project = stakeholder_project(key)
        configured = project.urls or {}
        return {"project": key, "urls": {"develop": str(configured.get("develop") or ""),
                                          "production": str(configured.get("production") or "")},
                "changes": stakeholder_changes(key)}

    @app.post("/v1/stakeholder/{key}/verdict")
    def stakeholder_verdict(key: str, body: StakeholderVerdictBody) -> dict:
        project = stakeholder_project(key)
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        sender = body.sender.strip()
        if not sender:
            raise HTTPException(400, "sender is required")
        change = ctx.db.one("SELECT key, summary, type, parent_key FROM tickets WHERE key=%s AND project=%s",
                            (body.change_key, key))
        if not change:
            raise HTTPException(400, "change must belong to this project")
        target = body.target_key.strip() or body.change_key
        if target != body.change_key and target != change["parent_key"]:
            raise HTTPException(400, "acceptance target must be the related ticket or its epic")
        if target != body.change_key and not ctx.db.one(
                "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'", (target, key)):
            raise HTTPException(400, "acceptance target must be the related epic")
        if body.verdict == "not_expected" and (not body.expected.strip() or not body.actual.strip()):
            raise HTTPException(400, "expected and actual behaviour are required for UAT feedback")

        screenshot = None
        if body.screenshot_data:
            match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", body.screenshot_data)
            if not match:
                raise HTTPException(400, "screenshot must be a PNG, JPEG, or WebP data URL")
            try:
                content = base64.b64decode(match.group(2), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(400, "screenshot data is invalid") from exc
            if not content or len(content) > 5_000_000:
                raise HTTPException(400, "screenshot must be between 1 byte and 5 MB")
            mime = match.group(1)
            filename = Path(body.screenshot_filename).name
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:200] or "screenshot"
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            if not filename.lower().endswith(ext):
                filename = filename.rsplit(".", 1)[0] + ext
            screenshot = (filename, content, mime)

        page_url = str((project.urls or {}).get(body.environment) or "").strip()
        page_label = page_url or "chưa deploy"
        release_commit = body.release_commit.strip() or "chưa có dữ liệu"
        if body.verdict == "expected":
            comment = (f"Comment nghiệm thu\nMôi trường: {body.environment}\n"
                       f"Release hoặc commit: {release_commit}\nNgười gửi: {sender}")
            try:
                comment_id = ctx.jira.comment(target, comment)
            except Exception as exc:  # Jira adapters and HTTP clients can raise different transport exceptions.
                log.warning("Stakeholder acceptance comment failed for %s", target)
                raise HTTPException(502, "Jira could not record the acceptance") from exc
            audit("jira", "stakeholder_acceptance", target, "", None, sender, True, comment_id,
                  {"project": key, "change": body.change_key, "environment": body.environment})
            ctx.events.emit("stakeholder.acceptance", ticket=target, project=key, outcome="accepted",
                            account=sender, reason=body.environment)
            return {"ok": True, "verdict": body.verdict, "ticket": target, "comment_id": comment_id}

        description = (f"Related change: {change['key']} - {change['summary']}\n"
                       f"Page URL: {page_label}\nEnvironment: {body.environment}\n"
                       f"Release or commit: {release_commit}\n"
                       f"Expected behaviour (mong đợi):\n{body.expected.strip()}\n\n"
                       f"Actual behaviour (thực tế):\n{body.actual.strip()}\n\n"
                       f"Sender (người gửi): {sender}")
        summary = f"[UAT {body.environment}] {change['key']}: {body.actual.strip().splitlines()[0][:120]}"
        try:
            issue_key = ctx.jira.create_issue(key, summary, description, issuetype="Bug", labels=["uat-bug"])
            status = ctx.jira.transition_to(issue_key, "Intake")
            if status != "Intake":
                raise RuntimeError("new UAT issue did not reach Intake")
            if screenshot:
                ctx.jira.add_attachment(issue_key, screenshot[0], screenshot[1], screenshot[2])
        except Exception as exc:  # Never tell the stakeholder a partial Jira write succeeded.
            log.warning("Stakeholder UAT issue creation failed for project %s", key)
            raise HTTPException(502, "Jira could not record the UAT feedback") from exc
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (issue_key, key, ["uat-bug"], summary, description))
        audit("jira", "stakeholder_bug", issue_key, "", None, sender, True, status,
              {"project": key, "change": body.change_key, "environment": body.environment,
               "screenshot": bool(screenshot)})
        ctx.events.emit("stakeholder.feedback", ticket=issue_key, project=key, outcome="created",
                        account=sender, reason=body.environment, data={"related": body.change_key})
        return {"ok": True, "verdict": body.verdict, "key": issue_key, "status": status}

    @app.get("/v1/state", responses={200: {"description": "Command Center, Delivery, and SA snapshot",
                                            "content": {"application/json": {"schema": STATE_RESPONSE_SCHEMA}}}})
    def state(request: Request, authorization: str = Header(default="")) -> dict:
        if authorization:  # the deploy scripts on the control machine read it with the owner token (login is on)
            owner_auth(request, authorization)
        from .state import snapshot
        # The login middleware authenticates console sessions and permits this legacy owner-token route;
        # resolving the cookie here keeps scoped session data filtered while allowing the owner token alone.
        user = optional_console_user(request)
        data = snapshot(ctx)
        all_tickets = data["tickets"]
        if user and user.project_keys is not None:
            visible = set(user.project_keys)
            data["projects"] = [project for project in data["projects"] if project["key"] in visible]
            data["tickets"] = [ticket for ticket in all_tickets if ticket["project"] in visible]
            ticket_keys = {ticket["key"] for ticket in data["tickets"]}
            data["active_runs"] = [run for run in data["active_runs"] if run.get("project") in visible]
            data["recent_runs"] = [run for run in data["recent_runs"] if run.get("ticket") in ticket_keys]
            data["events"] = [event for event in data["events"] if event.get("ticket") in ticket_keys]
            # Project level artifacts carry different shapes across releases; omit aggregates for restricted users.
            data["release_readiness"] = []
            data["release_progress"] = []
            data["rollout"] = {}
            data["totals"] = ctx.db.one(
                "SELECT count(*) FILTER (WHERE started_at > now()-interval '24 hours') AS runs_24h,"
                " coalesce(sum(cost_usd) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS cost_24h,"
                " coalesce(sum(tokens_in) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_in_24h,"
                " coalesce(sum(tokens_out) FILTER (WHERE started_at > now()-interval '24 hours'),0) AS tokens_out_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome='done') AS done_24h,"
                " count(*) FILTER (WHERE started_at > now()-interval '24 hours' AND outcome = ANY(%s)) AS failed_24h"
                " FROM runs WHERE project=ANY(%s)",
                (["failed", "blocked", "timeout", "lost"], list(visible)))
            data["totals"] = {key: int(value) if key in {"runs_24h", "tokens_in_24h", "tokens_out_24h",
                                                               "done_24h", "failed_24h"} else float(value)
                              for key, value in data["totals"].items()}
            visible_nodes = {node["name"] for node in data["nodes"] if visible.intersection(node.get("projects") or [])}
            data["nodes"] = [node for node in data["nodes"] if node["name"] in visible_nodes]
            data["accounts"] = [account for account in data["accounts"] if account["node"] in visible_nodes]
            data["incidents"] = [item for item in data["incidents"]
                                 if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["incidents_recent"] = [item for item in data["incidents_recent"]
                                         if item.get("subject") in visible or item.get("subject") in visible_nodes]
            data["sa"] = {}
            data["version_policy"] = {}
        if user and not has_permission(user.effective_roles, "view.machines_ai"):
            data["nodes"] = []
            data["accounts"] = []
            data["sa"] = {}
            data["role_health"] = {"roles": [], "settings": {}}
            data["version_policy"] = {}
            data["incidents"] = []
            data["incidents_recent"] = []
            data["events"] = [{key: event.get(key) for key in ("ts", "type", "ticket", "outcome", "reason")}
                               for event in data["events"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "ended_age_s")}
            data["active_runs"] = [{key: run[key] for key in ("id", "ticket", "project", "summary", "step",
                                                                  "elapsed_s", "heartbeat_age_s", "lease_ms", "progress")}
                                    for run in data["active_runs"]]
            data["recent_runs"] = [{key: run[key] for key in ("id", "ticket", "summary", "step", "outcome",
                                                                  "error_kind", "duration_s", "pr_url", "ended_age_s")}
                                    for run in data["recent_runs"]]
            for ticket in data["tickets"]:
                if ticket.get("run"):
                    ticket["run"] = {key: ticket["run"].get(key) for key in
                                     ("id", "ticket", "project", "summary", "step", "elapsed_s", "progress")}
                if ticket.get("last_run"):
                    ticket["last_run"] = {key: ticket["last_run"].get(key) for key in
                                          ("id", "ticket", "summary", "step", "outcome", "error_kind",
                                           "duration_s", "pr_url", "ended_age_s")}
        return data

    @app.get("/v1/deploy/state")
    def deploy_state(_deploy: str = Depends(deploy_auth)) -> dict:
        """The registry state for aiflow4-autodeploy; works with console sign-in on because it checks the token."""
        from .state import snapshot
        return snapshot(ctx)

    @app.get("/v1/deploy/ci-state")
    def deploy_ci_state(repo: str = Query(pattern=r"^[\w.-]+/[\w.-]+$"), sha: str = Query(pattern=r"^[0-9a-fA-F]{7,64}$"),
                        _deploy: str = Depends(deploy_auth)) -> dict:
        """CI state of a commit (pending|failed|ok|none) through the connection for its repository."""
        from .canary import ci_state
        gateway = github_gateway(ctx, repo)
        return {"repo": repo, "sha": sha, "state": ci_state(gateway, repo, sha)}

    @app.get("/v1/tickets/{key}/timeline")
    def ticket_timeline(key: str) -> dict:
        from .timeline import timeline
        tl = timeline(ctx.db, key)
        if tl is None:
            raise HTTPException(404, "unknown ticket")
        return tl

    @app.get("/v1/role-health")
    def role_health_state() -> dict:
        return role_health.summary(ctx)

    @app.put("/v1/role-health/settings")
    def role_health_settings(body: RoleHealthSettingsBody, request: Request) -> dict:
        user = console_user(request, {"console.manage"}, target="aiflow-control", action="role_health_settings")
        try:
            role_health.set_settings(ctx.db, body.window_runs, body.failure_percent, user.subject)
        except ValueError as e:
            audit("aiflow-control", "role_health_settings", "", "", None, user.subject, False, str(e), body.model_dump())
            raise HTTPException(422, str(e)) from e
        audit("aiflow-control", "role_health_settings", "", "", None, user.subject, True, "saved", body.model_dump())
        ctx.events.emit("role.settings", account=user.subject, outcome="saved",
                        reason=f"window {body.window_runs} runs, failure score above {body.failure_percent}%")
        role_health.evaluate(ctx)
        return role_health.summary(ctx)

    @app.get("/v1/analytics/daily")
    def analytics_daily(request: Request, days: int = Query(14, ge=1, le=90), project: str = "") -> dict:
        from .analytics import daily
        user = (console_user(request, "view.dashboard") if ctx.settings.console_login
                else optional_console_user(request))
        if user and user.project_keys is not None:
            if project:
                if project not in user.project_keys:
                    raise HTTPException(403, "not available for your role")
                projects = [project]
            else:
                projects = list(user.project_keys)
            rows = [row for key in projects for row in daily(ctx.db, days, key)]
        else:
            rows = daily(ctx.db, days, project)
        return {"days": days, "project": project, "rows": rows}

    @app.get("/v1/analytics/usage")
    def analytics_usage(request: Request, days: int = Query(7, ge=1, le=90),
                        group_by: Literal["account", "ai", "machine", "project", "ticket"] = "account",
                        project: str = "", ticket: str = "", account: str = "") -> dict:
        from .analytics import usage
        user = (console_user(request, None) if ctx.settings.console_login
                else optional_console_user(request))

        def refuse() -> None:
            if user:
                audit("console", "access_denied", "", "", None, user.subject, False,
                      "not available for your role", {"path": request.url.path, "method": request.method,
                                                       "roles": list(user.effective_roles)})
            raise HTTPException(403, "not available for your role")

        needs_machine_access = group_by in {"account", "machine"}
        if user and needs_machine_access and not has_permission(user.effective_roles, "view.machines_ai"):
            refuse()

        project_keys = None
        if user and user.project_keys is not None:
            project_keys = user.project_keys
            if project and project not in project_keys:
                refuse()
            if ticket:
                ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,))
                projects = ({ticket_row["project"]} if ticket_row else set())
                if not projects:
                    projects = {row["project"] for row in ctx.db.all(
                        "SELECT DISTINCT project FROM runs WHERE ticket_key=%s AND project<>''", (ticket,))}
                if projects and not projects.issubset(set(project_keys)):
                    refuse()

        return {"days": days, "group_by": group_by, "filters": {"project": project, "ticket": ticket,
                                                                     "account": account},
                "rows": usage(ctx.db, days, group_by, project, ticket, account, project_keys=project_keys)}

    # --- memory: past tickets, run summaries and verify findings ------------------------------------------------
    @app.get("/v1/memory/search")
    def memory_search(request: Request, q: str = "", project: str = "", limit: int = 10) -> dict:
        user = (console_user(request, "view.knowledge") if ctx.settings.console_login
                else optional_console_user(request))
        if user and user.project_keys is not None:
            if project:
                if project not in user.project_keys:
                    raise HTTPException(403, "not available for your role")
                keys = [project]
            else:
                keys = list(user.project_keys)
            hits = [hit for key in keys for hit in memory.search(ctx.db, key, q, limit)]
            return {"project": project, "q": q, "hits": hits[:limit]}
        return {"project": project, "q": q, "hits": memory.search(ctx.db, project, q, limit)}

    @app.get("/v1/memory/study-cases")
    def memory_study_cases(request: Request, project: str = "", limit: int = Query(50, ge=1, le=100)) -> dict:
        user = (console_user(request, "view.knowledge") if ctx.settings.console_login
                else optional_console_user(request))
        if user and user.project_keys is not None:
            if project:
                if project not in user.project_keys:
                    raise HTTPException(403, "not available for your role")
                keys = [project]
            else:
                keys = list(user.project_keys)
            cases = [case for key in keys for case in project_lifecycle.study_cases(ctx.db, key, limit)]
            return {"project": project, "study_cases": cases[:limit]}
        return {"project": project, "study_cases": project_lifecycle.study_cases(ctx.db, project, limit)}

    # --- learned knowledge: automatic capture, isolated context packs, and Knowledge screen data --------------
    @app.get("/v1/knowledge/items")
    def knowledge_items(request: Request, project: str = "", q: str = "", include_retired: bool = True) -> dict:
        console_user(request, "knowledge.read")
        return knowledge.dashboard(ctx.db, project, q, include_retired)

    @app.post("/v1/knowledge/items")
    def knowledge_item_save(body: KnowledgeItemBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_save")
        try:
            item = knowledge.save_item(ctx.db, project=body.project, scope=body.scope, epic=body.epic, kind=body.kind,
                                       title=body.title, content=body.content, actor=body.actor or user.subject)
        except knowledge.Invalid as exc:
            raise HTTPException(422, str(exc)) from exc
        return knowledge._pack_item(item)

    @app.post("/v1/knowledge/items/{item_id}/{action}")
    def knowledge_item_action(item_id: int, action: str, body: KnowledgeItemActionBody, request: Request) -> dict:
        user = console_user(request, "knowledge.manage", target="knowledge", action="item_" + action,
                            node=str(item_id))
        try:
            item = knowledge.moderate_item(ctx.db, item_id, action=action, actor=body.actor or user.subject,
                                           content=body.content, title=body.title,
                                           duplicate_id=body.duplicate_id, project=body.project,
                                           scope=body.scope or None, epic=body.epic)
        except knowledge.Invalid as exc:
            raise HTTPException(422 if "not found" not in str(exc) else 404, str(exc)) from exc
        return knowledge._pack_item(item)

    # --- Knowledge screen: versioned control-repo resources -------------------------------------------------
    def load_managed_resources() -> None:
        knowledge.import_local(ctx.db, ctx.repo().root)

    def submit_resource_change(kind: str, resource_id: str, title: str, content: str, archived: bool,
                               actor: str, action: str, expected_version: int | None = None) -> dict:
        try:
            kind, resource_id, title, content, actor = knowledge.validate(kind, resource_id, title, content, actor)
        except knowledge.Invalid as e:
            raise HTTPException(400, str(e)) from e
        github = github_control_gateway(ctx)
        load_managed_resources()
        current = knowledge.get_resource(ctx.db, kind, resource_id)
        if expected_version is not None and (not current or int(current["version"]) != expected_version):
            raise HTTPException(409, "resource changed since it was loaded; refresh and try again")
        path = knowledge.path_for(kind, resource_id)
        repo_name = Path(ctx.settings.control_repo).name
        payload = knowledge.file_content(kind, resource_id, title, content, archived)
        try:
            base = github.default_branch(repo_name)
            if kind == "policy":
                branch = f"aiflow/policy-{resource_id.lower()}-{secrets.token_hex(4)}"
                github.create_branch(repo_name, branch, base)
                sha = github.commit_file(repo_name, branch, path, payload,
                                             f"policy: propose {action} {resource_id}")
                pr = github.create_control_pr(repo_name, branch, base, f"Policy: {title}",
                                                  f"Policy {action} for `{resource_id}` proposed by {actor}.\n\n"
                                                  "Merge only after owner approval in the AI Flow Knowledge screen.")
                ctx.db.run("INSERT INTO managed_resource_proposals (resource_type,resource_id,title,content,archived,"
                           "base_title,base_content,base_archived,base_version,actor,action,branch,pr_number,pr_url)"
                           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (kind, resource_id, title, content, archived, current["title"] if current else "",
                            current["content"] if current else "", current["archived"] if current else False,
                            current["version"] if current else 0, actor, action, branch, int(pr["number"]),
                            str(pr.get("html_url") or "")))
                audit("aiflow-control", "policy-pr", "", "", None, actor, True, str(pr["html_url"]),
                      {"type": kind, "id": resource_id, "branch": branch, "pr": int(pr["number"]), "commit": sha})
                ctx.events.emit("resource.policy_proposed", project=resource_id if resource_id != "default" else "",
                                account=actor, outcome="pending", reason=f"PR #{pr['number']}",
                                data={"type": kind, "id": resource_id})
                return {"pending": True, "type": kind, "id": resource_id, "title": title,
                        "pr_number": int(pr["number"]), "pr_url": str(pr.get("html_url") or ""), "version":
                        int(current["version"]) if current else 0}

            sha = github.commit_file(repo_name, base, path, payload, f"{kind}: {action} {resource_id}")
        except HTTPException:
            raise
        except Exception as e:  # GitHub errors vary between App and transport implementations.
            log.warning("control repo write failed for %s/%s", kind, resource_id)
            raise HTTPException(502, "control repo could not save the resource") from e
        row = knowledge.save_current(ctx.db, kind=kind, resource_id=resource_id, title=title, content=content,
                                     archived=archived, actor=actor, action=action, path=path, commit_sha=sha)
        audit("aiflow-control", "resource-commit", "", "", None, actor, True, sha,
              {"type": kind, "id": resource_id, "version": row["version"], "path": path})
        ctx.events.emit("resource.changed", account=actor, outcome=action, reason=f"{kind}/{resource_id}",
                        data={"type": kind, "id": resource_id, "version": row["version"], "commit": sha})
        return row

    @app.get("/v1/knowledge/resources")
    def knowledge_resources(request: Request, include_archived: bool = True) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        return {"resources": knowledge.list_resources(ctx.db, include_archived),
                "proposals": knowledge.proposals(ctx.db)}

    @app.get("/v1/knowledge/resources/{kind}/{resource_id}/history")
    def knowledge_history(kind: str, resource_id: str, request: Request) -> dict:
        console_user(request, "knowledge.read")
        load_managed_resources()
        if kind not in knowledge.KINDS:
            raise HTTPException(404, "unknown resource type")
        return {"type": kind, "id": resource_id, "history": knowledge.history(ctx.db, kind, resource_id),
                "proposals": knowledge.proposals(ctx.db, kind, resource_id)}

    @app.post("/v1/knowledge/resources")
    def knowledge_save(body: KnowledgeBody, request: Request) -> dict:
        console_user(request, "knowledge.manage", target="aiflow-control", action="resource_save")
        if not body.id.strip():
            raise HTTPException(400, "resource id is required")
        action = "archive" if body.archived else "save"
        return submit_resource_change(body.type, body.id, body.title, body.content, body.archived, body.actor, action,
                                      body.version)

    @app.post("/v1/knowledge/resources/{kind}/{resource_id}/restore")
    def knowledge_restore(kind: str, resource_id: str, body: KnowledgeRestoreBody, request: Request) -> dict:
        console_user(request, "knowledge.manage", target="aiflow-control", action="resource_restore",
                     node=resource_id)
        load_managed_resources()
        version = ctx.db.one("SELECT title,content,archived FROM managed_resource_history WHERE resource_type=%s"
                             " AND resource_id=%s AND version=%s", (kind, resource_id, body.version))
        if not version:
            raise HTTPException(404, "resource version not found")
        return submit_resource_change(kind, resource_id, version["title"], version["content"], version["archived"],
                                      body.actor, "restore")

    @app.post("/v1/knowledge/proposals/{proposal_id}/approve")
    def knowledge_approve(proposal_id: int, body: KnowledgeApprovalBody,
                          _owner: str = Depends(owner_auth)) -> dict:
        proposal = ctx.db.one("SELECT * FROM managed_resource_proposals WHERE id=%s", (proposal_id,))
        if not proposal:
            raise HTTPException(404, "policy proposal not found")
        if proposal["status"] != "pending":
            raise HTTPException(409, "policy proposal is no longer pending")
        github = github_control_gateway(ctx)
        repo_name = Path(ctx.settings.control_repo).name
        owner = _owner or body.approved_by.strip() or "owner"
        try:
            merged = github.merge_pr(repo_name, int(proposal["pr_number"]),
                                         f"Policy: {proposal['title']}", "squash")
        except Exception as e:  # Do not expose provider responses or credentials through the UI.
            log.warning("policy proposal merge failed for PR #%s", proposal["pr_number"])
            raise HTTPException(502, "policy proposal could not be merged") from e
        if not merged.get("merged"):
            return {"merged": False, "message": merged.get("message", "GitHub refused the merge")}
        resource = knowledge.save_current(
            ctx.db, kind="policy", resource_id=proposal["resource_id"], title=proposal["title"],
            content=proposal["content"], archived=proposal["archived"], actor=proposal["actor"],
            action=proposal["action"], path=knowledge.path_for("policy", proposal["resource_id"]),
            commit_sha=str(merged.get("sha") or ""), approved_by=owner)
        ctx.db.run("UPDATE managed_resource_proposals SET status='merged',merged_at=now(),approved_by=%s WHERE id=%s",
                   (owner, proposal_id))
        audit("aiflow-control", "policy-merge", "", "", None, owner, True, str(merged.get("sha") or ""),
              {"id": proposal["resource_id"], "pr": proposal["pr_number"], "version": resource["version"]})
        ctx.events.emit("resource.policy_merged", project=proposal["resource_id"] if proposal["resource_id"] != "default" else "",
                        account=owner, outcome="merged", reason=f"PR #{proposal['pr_number']}",
                        data={"type": "policy", "id": proposal["resource_id"], "version": resource["version"]})
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception:
                log.warning("control repo refresh after policy merge failed")
        return {"merged": True, "sha": merged.get("sha", ""), "resource": resource}

    # --- encrypted backups and disaster recovery ----------------------------------------------------------
    def backup_config(include_history: bool = True) -> dict:
        row = ctx.db.one("SELECT * FROM control_backup_config WHERE id=true")
        if not row:
            raise HTTPException(503, "backup settings are not initialized")
        result = {key: value for key, value in row.items() if key not in {"id", "updated_at"}}
        for key, value in list(result.items()):
            if hasattr(value, "isoformat"):
                result[key] = value.isoformat()
        result["key_downloaded"] = bool(row["key_downloaded_at"])
        result["key_available"] = Path(ctx.settings.backup_key_file).is_file()
        if include_history:
            try:
                result["backups"] = control_backups.list_backups(result)
                result["storage_error"] = ""
            except control_backups.BackupError:
                result["backups"] = []
                result["storage_error"] = "Backup destination could not be listed; check its settings and credentials."
        return result

    @app.get("/v1/backups")
    def backup_status(request: Request) -> dict:
        console_user(request, "backup.read")
        return backup_config()

    @app.put("/v1/backups/config")
    def backup_configure(body: BackupConfigBody, request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="configure")
        if body.destination_kind in {"local", "mounted"}:
            requested = Path(body.destination_path.strip())
            if not requested.is_absolute() or requested == Path("/") or ".." in requested.parts:
                raise HTTPException(400, "backup folder must be an absolute folder path")
            destination = requested.resolve(strict=False)
            if body.destination_kind == "local":
                local_root = Path(ctx.settings.backup_path).resolve(strict=False)
                if destination != local_root and not destination.is_relative_to(local_root):
                    raise HTTPException(400, "local backups must be stored under the durable local backup folder")
            protected = [Path(ctx.settings.control_repo).resolve(strict=False),
                         Path(ctx.settings.control_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.console_secrets_dir).resolve(strict=False),
                         Path(ctx.settings.github_app_dir).resolve(strict=False),
                         Path(ctx.settings.jira_secret_dir).resolve(strict=False)]
            if any(destination == root or destination.is_relative_to(root) or root.is_relative_to(destination)
                   for root in protected):
                raise HTTPException(400, "backup folder must be separate from the control repository and secrets")
            path_value = str(destination)
            endpoint, bucket, prefix, region = "", "", "", "us-east-1"
        else:
            from urllib.parse import urlsplit
            parsed = urlsplit(body.s3_endpoint.strip())
            if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not body.s3_bucket.strip()):
                raise HTTPException(400, "enter an HTTPS S3-compatible endpoint and bucket")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,62}", body.s3_bucket.strip()):
                raise HTTPException(400, "S3 bucket name is invalid")
            endpoint, bucket = body.s3_endpoint.strip().rstrip("/"), body.s3_bucket.strip()
            prefix = "/".join(part for part in body.s3_prefix.strip().split("/") if part and part not in {".", ".."})
            region = body.s3_region.strip() or "us-east-1"
            path_value = "/srv/aiflow/backups"
        ctx.db.run("UPDATE control_backup_config SET schedule=%s,destination_kind=%s,destination_path=%s,s3_endpoint=%s,"
                   "s3_bucket=%s,s3_prefix=%s,s3_region=%s,retention_days=%s,updated_at=now() WHERE id=true",
                   (body.schedule, body.destination_kind, path_value, endpoint, bucket, prefix, region,
                    body.retention_days))
        audit("backup", "configure", "", "", None, user.subject, True, "backup settings updated",
              {"schedule": body.schedule, "destination": body.destination_kind, "retention_days": body.retention_days})
        return backup_config()

    @app.post("/v1/backups/key")
    def backup_key_download(request: Request) -> StreamingResponse:
        user = console_user(request, "backup.manage", target="backup", action="download_key")
        from fastapi import HTTPException as ApiError
        key_path = Path(ctx.settings.backup_key_file)
        try:
            with ctx.db.pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute("SELECT key_downloaded_at FROM control_backup_config WHERE id=true FOR UPDATE").fetchone()
                    if not row:
                        raise ApiError(503, "backup settings are not initialized")
                    if row["key_downloaded_at"] is not None or key_path.exists():
                        raise ApiError(409, "the backup encryption key has already been downloaded")
                    key = control_backups.create_key_file(key_path)
                    connection.execute("UPDATE control_backup_config SET key_downloaded_at=now(),updated_at=now() WHERE id=true")
        except ApiError:
            raise
        except OSError as exc:
            raise HTTPException(503, "the backup encryption key could not be created") from exc
        audit("backup", "download_key", "", "", None, user.subject, True, "one-time backup key download")
        return StreamingResponse(iter([key]), media_type="application/octet-stream",
                                  headers={"Content-Disposition": 'attachment; filename="aiflow4-backup-key"',
                                           "Cache-Control": "no-store", "Pragma": "no-cache",
                                           "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/backups/run")
    def backup_request(request: Request) -> dict:
        user = console_user(request, "backup.manage", target="backup", action="run_now")
        ctx.db.run("UPDATE control_backup_config SET backup_requested_at=now(),updated_at=now() WHERE id=true")
        audit("backup", "run_now", "", "", None, user.subject, True, "backup queued")
        return {"queued": True}

    jira_connect.mount(app, ctx)
    from .login import install_login
    install_login(app, ctx)
    return app
