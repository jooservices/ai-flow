"""Literal characterization tests for the console state snapshot section builders."""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aiflow.v4.control import state

NOW = datetime(2025, 1, 10, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2025, 1, 10, 12, 0, tzinfo=tz or UTC)


class SnapshotDB:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def all(self, query, params=None):
        label = _query_label(query)
        if label is None:
            raise AssertionError(f"unrecognized snapshot query: {query}")
        self.calls.append(label)
        return self.tables.get(label, [])

    def one(self, query, params=None):
        self.calls.append("totals")
        return self.tables.get("totals", {
            "runs": 0, "cost": 0, "tin": 0, "tout": 0, "done": 0, "failed": 0,
        })


def _query_label(query):
    return _run_query_label(query) or _account_query_label(query) or _system_query_label(query)


def _run_query_label(query):
    if "WHERE r.status='running'" in query:
        return "active_runs"
    if "WHERE r.status<>'running'" in query:
        return "recent_runs"
    if "SELECT DISTINCT ON (ticket_key)" in query:
        return "already_done"
    if "SELECT key, project" in query:
        return "tickets"
    return None


def _account_query_label(query):
    if "FROM accounts ORDER" in query:
        return "accounts"
    if "GROUP BY account_id" in query:
        return "today_by_account"
    if "SELECT id,sign_in_status" in query:
        return "sign_in"
    if "SELECT id,name FROM managed_ai_accounts" in query:
        return "account_names"
    if "FROM shared_accounts" in query:
        return "shared_managed"
    return None


def _system_query_label(query):
    if "machine_host_load_limits" in query:
        return "host_load"
    if "FROM nodes WHERE" in query:
        return "nodes"
    if "closed_at IS NULL" in query:
        return "incidents_open"
    if "closed_at > now()" in query:
        return "incidents_recent"
    if "FROM events" in query:
        return "events"
    if "FROM project_lifecycle" in query:
        return "lifecycle"
    return None


class SnapshotLeases:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def cooling_many(self, account_ids):
        self.calls.append(("cooling_many", list(account_ids)))
        return self.tables.get("cooldowns", {})

    def ttl_ms_many(self, ticket_keys):
        self.calls.append(("ttl_ms_many", list(ticket_keys)))
        return self.tables.get("lease_ttls", [])


class SnapshotRepo:
    def __init__(self, tables):
        self.tables = tables
        self.accounts = tables.get("repo_accounts", [])
        self.projects = tables.get("projects", {})
        self.nodes = tables.get("node_configs", {})
        self.inventory_in_db = tables.get("inventory_in_db", True)
        self.versions = {"tool": "1.0"}
        self.ref = "example/control"

    def projects_for_node(self, name, projects, dynamic):
        return self.tables.get("node_projects", {}).get(name, projects or [])

    def tools_for_node(self, name, projects):
        return self.tables.get("required_tools", {}).get(name, [])

    def account(self, account_id):
        return self.tables.get("account_configs", {}).get(str(account_id))

    def model_for(self, provider, tier):
        return (f"{provider}-model", f"{tier}-effort")


class SnapshotContext:
    def __init__(self, tables):
        self.db = SnapshotDB(tables)
        self.leases = SnapshotLeases(tables)
        self.settings = SimpleNamespace(
            env="test", gatus_url="https://status.example", control_version="4.1.0",
            version_max_minor_lag=2,
        )
        self._repo = SnapshotRepo(tables)

    def repo(self):
        return self._repo


def _node(**changes):
    row = {
        "name": "node-a", "ip": "192.0.2.10", "last_heartbeat": NOW,
        "status": "online", "role": "worker", "projects": [], "dynamic": False, "tools": [],
        "metrics": {}, "load_limits": {}, "load_state": None, "accepting_work": True,
        "slots_used": 0, "slots_total": 2, "release_version": "4.1.0",
        "token_hash": "token-hash", "created_at": NOW, "idle_since": None,
    }
    return {**row, **changes}


def _account_row(**changes):
    row = {
        "id": "acct-1", "name": "", "provider": "codex", "tier": "middle",
        "status": "ready", "credential_expires_at": None, "cooldown_until": None,
        "cooldown_since": None, "idle_since": None, "worker_state": {}, "quota": {},
        "quota_source": None, "last_error": "", "node": "node-a",
    }
    return {**row, **changes}


def _account_config(**changes):
    values = {
        "machines": ["node-a"], "paused": False, "sessions": 1,
        "roles": ["dev"], "explicit_only": False,
    }
    return SimpleNamespace(**{**values, **changes})


def _active_run(**changes):
    row = {
        "id": "run-1", "ticket_key": "PRJ-1", "project": "PRJ", "summary": "Build feature",
        "step": "dev", "account_id": "acct-1", "provider": "codex", "model": "model-a",
        "node": "node-a", "fence": 3, "started_at": NOW - timedelta(minutes=4),
        "heartbeat_at": NOW - timedelta(seconds=12), "progress": {"message": "working"},
    }
    return {**row, **changes}


def _recent_run(**changes):
    row = {
        "id": "run-old", "ticket_key": "PRJ-1", "project": "PRJ", "summary": "Build feature",
        "step": "dev", "account_id": "acct-1", "provider": "codex", "model": "model-a",
        "node": "node-a", "outcome": "done", "status": "done", "error_kind": "",
        "started_at": NOW - timedelta(minutes=12), "ended_at": NOW - timedelta(minutes=2),
        "cost_usd": 0.25, "tokens_in": 12, "tokens_out": 34, "pr_url": "https://example/pr/1",
        "details": {"already_done": False},
    }
    return {**row, **changes}


def _ticket(**changes):
    row = {
        "key": "PRJ-1", "project": "PRJ", "status": "To Do", "type": "Task",
        "priority": "Medium", "labels": [], "summary": "Build feature", "parent_key": None,
        "blocked_by": [], "done_at": None, "jira_updated": NOW - timedelta(hours=1),
    }
    return {**row, **changes}


def _project(**changes):
    values = {
        "key": "PRJ", "repo": "example/project", "configured_repositories": ["example/project"],
        "base": "main", "environments": {}, "required_services": [], "tools": [], "stacks": [],
        "work_minutes": lambda: 30,
        "repo_for": lambda ticket: "example/project",
    }
    return SimpleNamespace(**{**values, **changes})


def _node_cases(project):
    return [
        ("heartbeat_at_offline_boundary", {"nodes": [_node(last_heartbeat=NOW - timedelta(seconds=75))]}),
        ("heartbeat_over_offline_boundary", {"nodes": [_node(last_heartbeat=NOW - timedelta(seconds=76))]}),
        ("online_without_heartbeat", {"nodes": [_node(last_heartbeat=None)]}),
        ("explicitly_offline_node", {"nodes": [_node(status="offline")]}),
        ("legacy_inventory_and_stack_gaps", {
            "inventory_in_db": False, "projects": {"PRJ": project},
            "node_configs": {"node-a": SimpleNamespace(
                projects=["PRJ"], services=[{"unit": "api", "healthcheck": "curl"}, "bad"],
                purpose="batch", notes="legacy", capabilities=["gpu"],
            )},
            "nodes": [_node(projects=[], tools=[])], "node_projects": {"node-a": ["PRJ"]},
            "required_tools": {"node-a": ["node"]},
        }),
        ("declared_service_health_and_unknown_service", {
            "projects": {"PRJ": _project(required_services=["postgres", "redis"])},
            "nodes": [_node(projects=["PRJ"], metrics={"services": {
                "postgres": {"state": "running", "endpoint": "db:5432", "image": "pg", "detail": ""},
                "redis": {"state": "stopped", "detail": "unavailable"},
                "unknown": {"state": "running"},
            }})],
        }),
    ]


def _account_cases(good_account, account_config):
    return [
        ("paused_account", {
            "accounts": [good_account], "account_configs": {"acct-1": _account_config(paused=True)},
        }),
        ("signed_out_account", {
            "accounts": [_account_row(status="signed_out")], "account_configs": {"acct-1": account_config},
        }),
        ("cooling_account", {
            "accounts": [good_account], "account_configs": {"acct-1": account_config},
            "cooldowns": {"acct-1": ("quota", 42)},
        }),
        ("account_with_active_run", {
            "accounts": [good_account], "account_configs": {"acct-1": account_config},
            "active_runs": [_active_run()],
        }),
        ("account_on_offline_machine", {
            "accounts": [good_account], "account_configs": {"acct-1": _account_config(machines=["missing"])},
        }),
        ("error_probe_and_provider_status", {
            "accounts": [_account_row(status="error", worker_state={"probe": {"ok": False}})],
            "account_configs": {"acct-1": account_config},
        }),
        ("credential_expiry_at_warning_boundary", {
            "accounts": [_account_row(credential_expires_at=NOW + timedelta(days=7))],
            "account_configs": {"acct-1": account_config},
        }),
        ("quota_windows_and_resets", {
            "accounts": [_account_row(quota_source="provider", quota={"windows": {
                "day": {"utilization": 83.5, "resets_at": "2025-01-11T00:00:00Z"},
                "week": {"utilization": None, "resets_at": "2025-01-17T00:00:00Z"},
            }})], "account_configs": {"acct-1": account_config},
            "today_by_account": [{"account_id": "acct-1", "tokens_in": 10, "tokens_out": 20, "cost_usd": 1.25}],
        }),
        ("shared_account_with_signin", {
            "accounts": [good_account], "account_configs": {"acct-1": _account_config(
                machines=["node-a", "node-b"], sessions=2,
                roles=["dev", "verify", "intake_refiner", "master"], explicit_only=True,
            )}, "repo_accounts": [account_config], "sign_in": [{"id": "acct-1", "sign_in_status": "pending"}],
            "account_names": [{"id": "acct-1", "name": "Ada"}], "shared_managed": [{"id": "acct-1"}],
        }),
    ]


def _run_cases():
    return [
        ("active_and_recent_runs_with_lease", {
            "active_runs": [_active_run()], "lease_ttls": [1200], "recent_runs": [
                _recent_run(ended_at=None, outcome=None, status="failed", details=None),
            ], "totals": {"runs": 4, "cost": 1.5, "tin": 50, "tout": 60, "done": 2, "failed": 1},
        }),
        ("recent_run_with_duration_and_fallback_outcome", {
            "recent_runs": [_recent_run(outcome="", status="timeout", ended_at=NOW - timedelta(seconds=3))],
        }),
    ]


def _ticket_cases(project):
    return [
        ("verified_ticket_with_success_marker_and_complete_project", {
            "projects": {"PRJ": project}, "tickets": [_ticket(status="In Verify", parent_key="PRJ-9")],
            "already_done": [{"id": "run-done", "ticket_key": "PRJ-1", "pr_url": "https://example/pr/9",
                              "details": {"already_done": True}}],
            "recent_runs": [_recent_run()],
        }),
        ("verified_ticket_without_success_marker", {
            "projects": {"PRJ": project}, "tickets": [_ticket(status="In Verify")],
            "already_done": [{"id": "run-no-marker", "ticket_key": "PRJ-1", "pr_url": "", "details": {}}],
        }),
        ("closed_project_and_done_ticket", {
            "projects": {"PRJ": project}, "tickets": [_ticket(status="Done", done_at=NOW)],
            "lifecycle": [{"project": "PRJ", "status": "closed", "closed_at": NOW,
                           "closed_by": "owner", "version": 4}],
        }),
    ]


def _case_table():
    project = _project()
    good_account = _account_row()
    account_config = _account_config()
    cases = [("empty", {})]
    cases.extend(_node_cases(project))
    cases.extend(_account_cases(good_account, account_config))
    cases.extend(_run_cases())
    cases.extend(_ticket_cases(project))
    return [{"name": name, "tables": tables, "include_signin": name == "shared_account_with_signin"}
            for name, tables in cases]


def _stub_snapshot_dependencies(monkeypatch, tables):
    monkeypatch.setattr(state, "datetime", FrozenDateTime)
    monkeypatch.setattr(state, "thresholds", lambda repo: {"credential_expiry_days": 7})
    monkeypatch.setattr(state.flow_doctor, "dashboard", lambda ctx: {})
    monkeypatch.setattr(state.flow_doctor, "operator_intervention_summary", lambda ctx: {})
    monkeypatch.setattr(state.tooling, "waiting_summary", lambda ctx: [])
    monkeypatch.setattr(state, "dashboard_data", lambda ctx, nodes, accounts: {
        "node_names": [node["name"] for node in nodes], "account_ids": [account["id"] for account in accounts],
    })
    monkeypatch.setattr(state.role_health, "summary", lambda ctx: {})
    monkeypatch.setattr(state, "machines_views", lambda db, accounts, statuses, include_signin=False: {
        str(account["id"]): [{"include_signin": include_signin}] for account in tables.get("accounts", [])
    })
    monkeypatch.setattr(state, "missing_tools", lambda present, required: sorted(set(required) - set(present)))
    monkeypatch.setattr(state, "toolchain_gaps", lambda *args, **kwargs: [])
    monkeypatch.setattr(state, "epic_for_ticket", lambda ticket, index: ticket.get("parent_key") or "")
    monkeypatch.setattr(state.load_policy, "host_name", lambda metrics: (metrics or {}).get("hostname", ""))
    monkeypatch.setattr(state.load_policy, "machine_limits", lambda limits: limits or {})
    monkeypatch.setattr(state.load_policy, "host_limits", lambda limits: limits or {})


def _snapshot(case, monkeypatch):
    tables = case["tables"]
    ctx = SnapshotContext(tables)
    _stub_snapshot_dependencies(monkeypatch, tables)
    with ExitStack() as stack:
        stack.enter_context(patch("aiflow.v4.control.release_readiness.all_versions", return_value=[]))
        stack.enter_context(patch("aiflow.v4.control.release_progress.all_versions", return_value=[]))
        stack.enter_context(patch("aiflow.v4.control.scale_proof.summary", return_value={}))
        stack.enter_context(patch(
            "aiflow.v4.control.rollout.node_policy", side_effect=lambda *args: {"policy": args[1]},
        ))
        stack.enter_context(patch("aiflow.v4.control.rollout.summary", return_value={}))
        result = state.snapshot(ctx, include_signin=case["include_signin"])
    return result, ctx


# Captured from the unchanged snapshot with the deterministic contexts above.
BASELINE_EXPECTED = json.loads(r"""
{
  "generated_at": "2025-01-10T12:00:00+00:00",
  "env": "test",
  "control_repo": "example/control",
  "versions": {
    "tool": "1.0"
  },
  "version": "4.0.0a1",
  "links": {
    "o2": "/o2/",
    "mail": "/mail/",
    "gatus": "https://status.example"
  },
  "flow_doctor": {},
  "operator_interventions": {},
  "tool_waits": [],
  "nodes": [],
  "accounts": [],
  "active_runs": [],
  "recent_runs": [],
  "projects": [],
  "tickets": [],
  "release_readiness": [],
  "release_progress": [],
  "scale_proof": {},
  "incidents": [],
  "incidents_recent": [],
  "events": [],
  "sa": {
    "node_names": [],
    "account_ids": []
  },
  "role_health": {},
  "rollout": {},
  "version_policy": {
    "control": "4.1.0",
    "max_minor_lag": 2
  },
  "totals": {
    "runs_24h": 0,
    "cost_24h": 0.0,
    "tokens_in_24h": 0,
    "tokens_out_24h": 0,
    "done_24h": 0,
    "failed_24h": 0
  }
}
""")
EXPECTED_PATCHES = json.loads(r"""
{
  "heartbeat_at_offline_boundary": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [],
        "role": "worker",
        "status": "online",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {},
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [],
        "desired_services": [],
        "missing_tools": [],
        "heartbeat_age_s": 75,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": "2025-01-10T11:58:45+00:00",
        "idle_since": null,
        "purpose": "",
        "notes": "",
        "capabilities": [],
        "projects": [],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "heartbeat_over_offline_boundary": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [],
        "role": "worker",
        "status": "offline",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {},
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [],
        "desired_services": [],
        "missing_tools": [],
        "heartbeat_age_s": 76,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": "2025-01-10T11:58:44+00:00",
        "idle_since": null,
        "purpose": "",
        "notes": "",
        "capabilities": [],
        "projects": [],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "online_without_heartbeat": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [],
        "role": "worker",
        "status": "offline",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {},
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [],
        "desired_services": [],
        "missing_tools": [],
        "heartbeat_age_s": null,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": null,
        "idle_since": null,
        "purpose": "",
        "notes": "",
        "capabilities": [],
        "projects": [],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "explicitly_offline_node": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [],
        "role": "worker",
        "status": "offline",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {},
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [],
        "desired_services": [],
        "missing_tools": [],
        "heartbeat_age_s": 0,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": "2025-01-10T12:00:00+00:00",
        "idle_since": null,
        "purpose": "",
        "notes": "",
        "capabilities": [],
        "projects": [],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "legacy_inventory_and_stack_gaps": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [],
        "role": "worker",
        "status": "online",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {},
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [
          "node"
        ],
        "desired_services": [
          {
            "name": "api",
            "unit": "api",
            "healthcheck_configured": true
          }
        ],
        "missing_tools": [
          "node"
        ],
        "heartbeat_age_s": 0,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": "2025-01-10T12:00:00+00:00",
        "idle_since": null,
        "purpose": "batch",
        "notes": "legacy",
        "capabilities": [
          "gpu"
        ],
        "projects": [
          "PRJ"
        ],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "projects": [
      {
        "key": "PRJ",
        "repo": "example/project",
        "repositories": [
          "example/project"
        ],
        "base": "main",
        "environments": {},
        "work_minutes": 30,
        "counts": {},
        "status": "open",
        "closed": false,
        "closed_at": null,
        "closed_by": "",
        "report_version": 0,
        "close_suggested": false
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "declared_service_health_and_unknown_service": {
    "nodes": [
      {
        "name": "node-a",
        "ip": "192.0.2.10",
        "services": [
          {
            "name": "postgres",
            "projects": [
              "PRJ"
            ],
            "healthy": false,
            "state": "running",
            "endpoint": "db:5432",
            "image": "pg",
            "detail": "running"
          },
          {
            "name": "redis",
            "projects": [
              "PRJ"
            ],
            "healthy": false,
            "state": "stopped",
            "endpoint": "127.0.0.1:16399",
            "image": "",
            "detail": "unavailable"
          }
        ],
        "role": "worker",
        "status": "online",
        "accepting_work": true,
        "sessions_busy": 0,
        "slots_total": 2,
        "slots_used": 0,
        "release": "4.1.0",
        "metrics": {
          "services": {
            "postgres": {
              "state": "running",
              "endpoint": "db:5432",
              "image": "pg",
              "detail": ""
            },
            "redis": {
              "state": "stopped",
              "detail": "unavailable"
            },
            "unknown": {
              "state": "running"
            }
          }
        },
        "tools": [],
        "load_limits": {},
        "load_state": {
          "mode": "normal",
          "reason": ""
        },
        "host_load_limits": {},
        "host_load_state": {
          "mode": "normal",
          "reason": ""
        },
        "has_agent": true,
        "required_tools": [],
        "desired_services": [],
        "missing_tools": [],
        "heartbeat_age_s": 0,
        "accounts_assigned": 0,
        "in_control_repo": true,
        "last_heartbeat": "2025-01-10T12:00:00+00:00",
        "idle_since": null,
        "purpose": "",
        "notes": "",
        "capabilities": [],
        "projects": [
          "PRJ"
        ],
        "version_policy": {
          "policy": "node-a"
        },
        "joined_at": "2025-01-10T12:00:00+00:00"
      }
    ],
    "projects": [
      {
        "key": "PRJ",
        "repo": "example/project",
        "repositories": [
          "example/project"
        ],
        "base": "main",
        "environments": {},
        "work_minutes": 30,
        "counts": {},
        "status": "open",
        "closed": false,
        "closed_at": null,
        "closed_by": "",
        "report_version": 0,
        "close_suggested": false
      }
    ],
    "sa": {
      "node_names": [
        "node-a"
      ],
      "account_ids": []
    }
  },
  "paused_account": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": true,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "stopped",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "signed_out_account": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "signed_out",
        "provider_status": "signed_out",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "cooling_account": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "cooldown",
        "provider_status": "cooldown",
        "cooldown": {
          "reason": "quota",
          "seconds_left": 42
        },
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "account_with_active_run": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "working",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 1,
        "parallel": 1,
        "run": {
          "id": "run-1",
          "ticket": "PRJ-1",
          "summary": "Build feature",
          "step": "dev",
          "elapsed_s": 240,
          "fence": 3,
          "progress": {
            "message": "working"
          }
        }
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "account_on_offline_machine": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "missing",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "node_offline",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "error_probe_and_provider_status": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "error",
        "provider_status": "error",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": true,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {
          "probe": {
            "ok": false
          }
        },
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "credential_expiry_at_warning_boundary": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "node_offline",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "2025-01-17T12:00:00+00:00",
        "credential_expiry_warning": true,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "quota_windows_and_resets": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev"
        ],
        "machines": [
          {
            "include_signin": false
          }
        ],
        "session_limit": 1,
        "shared": false,
        "shared_managed": false,
        "explicit_only": false,
        "paused": false,
        "jobs": [
          "develop"
        ],
        "sign_in_pending": false,
        "sign_in_status": "idle",
        "status": "node_offline",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {
          "windows": {
            "day": {
              "utilization": 83.5,
              "resets_at": "2025-01-11T00:00:00Z"
            },
            "week": {
              "utilization": null,
              "resets_at": "2025-01-17T00:00:00Z"
            }
          }
        },
        "quota_awareness": {},
        "quota_used": 83.5,
        "quota_resets_at": "2025-01-11T00:00:00Z",
        "tokens_today": 30,
        "cost_today": 1.25,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 1,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "shared_account_with_signin": {
    "accounts": [
      {
        "id": "acct-1",
        "name": "Ada",
        "provider": "codex",
        "node": "node-a",
        "tier": "middle",
        "roles": [
          "dev",
          "verify",
          "intake_refiner",
          "master"
        ],
        "machines": [
          {
            "include_signin": true
          }
        ],
        "session_limit": 2,
        "shared": true,
        "shared_managed": true,
        "explicit_only": true,
        "paused": false,
        "jobs": [
          "develop",
          "verify",
          "intake",
          "master"
        ],
        "sign_in_pending": true,
        "sign_in_status": "pending",
        "status": "node_offline",
        "provider_status": "ready",
        "cooldown": null,
        "cooldown_until": null,
        "cooldown_since": null,
        "idle_since": null,
        "credential_expires_at": "unknown",
        "credential_expiry_warning": false,
        "check_failed": false,
        "quota": {},
        "quota_awareness": {},
        "quota_used": null,
        "quota_resets_at": null,
        "tokens_today": 0,
        "cost_today": 0.0,
        "last_error": "",
        "worker": {},
        "model": "codex-model",
        "effort": "middle-effort",
        "runs_active": 0,
        "parallel": 2,
        "run": null
      }
    ],
    "sa": {
      "node_names": [],
      "account_ids": [
        "acct-1"
      ]
    }
  },
  "active_and_recent_runs_with_lease": {
    "active_runs": [
      {
        "id": "run-1",
        "ticket": "PRJ-1",
        "project": "PRJ",
        "summary": "Build feature",
        "step": "dev",
        "account": "acct-1",
        "provider": "codex",
        "model": "model-a",
        "node": "node-a",
        "fence": 3,
        "elapsed_s": 240,
        "heartbeat_age_s": 12,
        "lease_ms": 1200,
        "progress": {
          "message": "working"
        }
      }
    ],
    "recent_runs": [
      {
        "id": "run-old",
        "ticket": "PRJ-1",
        "summary": "Build feature",
        "step": "dev",
        "account": "acct-1",
        "provider": "codex",
        "model": "model-a",
        "node": "node-a",
        "outcome": "failed",
        "error_kind": "",
        "duration_s": null,
        "cost_usd": 0.25,
        "tokens_in": 12,
        "tokens_out": 34,
        "pr_url": "https://example/pr/1",
        "already_done": false,
        "ended_age_s": null
      }
    ],
    "totals": {
      "runs_24h": 4,
      "cost_24h": 1.5,
      "tokens_in_24h": 50,
      "tokens_out_24h": 60,
      "done_24h": 2,
      "failed_24h": 1
    }
  },
  "recent_run_with_duration_and_fallback_outcome": {
    "recent_runs": [
      {
        "id": "run-old",
        "ticket": "PRJ-1",
        "summary": "Build feature",
        "step": "dev",
        "account": "acct-1",
        "provider": "codex",
        "model": "model-a",
        "node": "node-a",
        "outcome": "timeout",
        "error_kind": "",
        "duration_s": 717,
        "cost_usd": 0.25,
        "tokens_in": 12,
        "tokens_out": 34,
        "pr_url": "https://example/pr/1",
        "already_done": false,
        "ended_age_s": 3
      }
    ]
  },
  "verified_ticket_with_success_marker_and_complete_project": {
    "recent_runs": [
      {
        "id": "run-old",
        "ticket": "PRJ-1",
        "summary": "Build feature",
        "step": "dev",
        "account": "acct-1",
        "provider": "codex",
        "model": "model-a",
        "node": "node-a",
        "outcome": "done",
        "error_kind": "",
        "duration_s": 600,
        "cost_usd": 0.25,
        "tokens_in": 12,
        "tokens_out": 34,
        "pr_url": "https://example/pr/1",
        "already_done": false,
        "ended_age_s": 120
      }
    ],
    "projects": [
      {
        "key": "PRJ",
        "repo": "example/project",
        "repositories": [
          "example/project"
        ],
        "base": "main",
        "environments": {},
        "work_minutes": 30,
        "counts": {
          "In Verify": 1
        },
        "status": "open",
        "closed": false,
        "closed_at": null,
        "closed_by": "",
        "report_version": 0,
        "close_suggested": false
      }
    ],
    "tickets": [
      {
        "key": "PRJ-1",
        "project": "PRJ",
        "status": "In Verify",
        "type": "Task",
        "priority": "Medium",
        "labels": [],
        "summary": "Build feature",
        "parent_key": "PRJ-9",
        "blocked_by": [],
        "done_at": null,
        "epic": "PRJ-9",
        "repository": "example/project",
        "already_done": {
          "pr_url": "https://example/pr/9",
          "run_id": "run-done"
        },
        "run": null,
        "last_run": {
          "id": "run-old",
          "ticket": "PRJ-1",
          "summary": "Build feature",
          "step": "dev",
          "account": "acct-1",
          "provider": "codex",
          "model": "model-a",
          "node": "node-a",
          "outcome": "done",
          "error_kind": "",
          "duration_s": 600,
          "cost_usd": 0.25,
          "tokens_in": 12,
          "tokens_out": 34,
          "pr_url": "https://example/pr/1",
          "already_done": false,
          "ended_age_s": 120
        },
        "updated_age_s": 3600
      }
    ]
  },
  "verified_ticket_without_success_marker": {
    "projects": [
      {
        "key": "PRJ",
        "repo": "example/project",
        "repositories": [
          "example/project"
        ],
        "base": "main",
        "environments": {},
        "work_minutes": 30,
        "counts": {
          "In Verify": 1
        },
        "status": "open",
        "closed": false,
        "closed_at": null,
        "closed_by": "",
        "report_version": 0,
        "close_suggested": false
      }
    ],
    "tickets": [
      {
        "key": "PRJ-1",
        "project": "PRJ",
        "status": "In Verify",
        "type": "Task",
        "priority": "Medium",
        "labels": [],
        "summary": "Build feature",
        "parent_key": null,
        "blocked_by": [],
        "done_at": null,
        "epic": "",
        "repository": "example/project",
        "already_done": null,
        "run": null,
        "last_run": null,
        "updated_age_s": 3600
      }
    ]
  },
  "closed_project_and_done_ticket": {
    "projects": [
      {
        "key": "PRJ",
        "repo": "example/project",
        "repositories": [
          "example/project"
        ],
        "base": "main",
        "environments": {},
        "work_minutes": 30,
        "counts": {
          "Done": 1
        },
        "status": "closed",
        "closed": true,
        "closed_at": "2025-01-10T12:00:00+00:00",
        "closed_by": "owner",
        "report_version": 4,
        "close_suggested": false
      }
    ],
    "tickets": [
      {
        "key": "PRJ-1",
        "project": "PRJ",
        "status": "Done",
        "type": "Task",
        "priority": "Medium",
        "labels": [],
        "summary": "Build feature",
        "parent_key": null,
        "blocked_by": [],
        "done_at": "2025-01-10T12:00:00+00:00",
        "epic": "",
        "repository": "example/project",
        "already_done": null,
        "run": null,
        "last_run": null,
        "updated_age_s": 3600
      }
    ]
  }
}
""")

@pytest.mark.parametrize("case", _case_table(), ids=lambda case: case["name"])
def test_snapshot_matches_pre_refactor_literal(case, monkeypatch):
    actual, ctx = _snapshot(case, monkeypatch)
    expected = dict(BASELINE_EXPECTED)
    expected.update(EXPECTED_PATCHES.get(case["name"], {}))
    actual_json = json.dumps(actual, separators=(",", ":"), default=lambda item: item.isoformat())
    assert actual_json == json.dumps(expected, separators=(",", ":"))
    expected_calls = [
        "active_runs", "host_load", "nodes", "accounts", "today_by_account", "sign_in", "account_names",
        "shared_managed", "recent_runs", "tickets",
    ]
    if case["tables"].get("tickets") and any(row["status"] == "In Verify" for row in case["tables"]["tickets"]):
        expected_calls.append("already_done")
    expected_calls.extend(["totals", "incidents_open", "incidents_recent", "events", "lifecycle"])
    assert ctx.db.calls == expected_calls
    assert [call[0] for call in ctx.leases.calls] == ["cooling_many", "ttl_ms_many"]


def test_node_builders_on_fakes(monkeypatch):
    _stub_snapshot_dependencies(monkeypatch, {})
    ctx = SnapshotContext({})
    repo = ctx.repo()
    assert state._active_run_context(ctx.db, repo) == ({}, [], {}, {}, {})
    assert state._node_service_views(repo, [], {}) == []
    with patch("aiflow.v4.control.rollout.node_policy", return_value={"release": "4.1.0"}):
        node = state._node_view(ctx, repo, {}, {}, _node(), {}, {}, {})
    assert node["status"] == "online" and node["heartbeat_age_s"] == 0
    assert state._node_section(ctx, ctx.db, repo, {}, {}) == ([], {}, [], {}, {})


def test_account_builders_on_fakes(monkeypatch):
    row = _account_row()
    tables = {"accounts": [row], "account_configs": {"acct-1": _account_config()}}
    _stub_snapshot_dependencies(monkeypatch, tables)
    ctx = SnapshotContext(tables)
    monkeypatch.setattr(state, "machines_views", lambda *args, **kwargs: {})
    monkeypatch.setattr(state, "datetime", FrozenDateTime)
    account = ctx.repo().account("acct-1")
    assert state._account_status(row, account, None, None, {"node-a": "online"}) == "ready"
    assert state._account_status(row, _account_config(paused=True), None, None, {}) == "stopped"
    assert state._account_quota(row) == ({}, [], [])
    data = state._account_data(ctx, ctx.db, ctx.repo(), {"node-a": "online"}, False)
    view = state._account_view(row, ctx.repo(), {"credential_expiry_days": 7}, {"node-a": "online"},
                               {}, {}, *data[1:])
    assert view["status"] == "ready" and view["quota_used"] is None
    assert state._account_section(ctx, ctx.db, ctx.repo(), {"node-a": "online"}, {"credential_expiry_days": 7},
                                  {}, {}, False)[0]["id"] == "acct-1"


def test_run_builders_on_fakes():
    active = _active_run()
    recent = _recent_run()
    ctx = SnapshotContext({"lease_ttls": [1200], "recent_runs": [recent]})
    assert state._active_run_view(active, 1200)["lease_ms"] == 1200
    assert state._active_runs_section(ctx, [active])[0]["ticket"] == "PRJ-1"
    ctx.leases.tables["lease_ttls"] = []
    assert state._active_runs_section(ctx, [active]) == []
    assert state._recent_run_view(recent)["duration_s"] == 600
    assert state._recent_runs_section(ctx.db)[0]["outcome"] == "done"


def test_ticket_and_project_builders_on_fakes(monkeypatch):
    project = _project()
    ticket = _ticket(status="In Verify", parent_key="PRJ-9")
    tables = {"projects": {"PRJ": project}, "tickets": [ticket], "already_done": [
        {"id": "run-done", "ticket_key": "PRJ-1", "pr_url": "https://example/pr/9",
         "details": {"already_done": True}},
    ]}
    _stub_snapshot_dependencies(monkeypatch, tables)
    ctx = SnapshotContext(tables)
    repo = ctx.repo()
    base, counts = state._ticket_base_rows(ctx.db, repo)
    assert counts == {"PRJ": {"In Verify": 1}} and base[0]["repository"] == "example/project"
    assert state._verified_run_markers(ctx.db, base)["PRJ-1"]["run_id"] == "run-done"
    rows, status_counts = state._ticket_section(SnapshotDB(tables), repo, [], [])
    assert rows[0]["already_done"]["run_id"] == "run-done" and status_counts == counts
    projects = state._project_section(repo, {"PRJ": {"Done": 1}}, {})
    assert projects[0]["close_suggested"] is True
    operational = state._operational_section(SnapshotDB({"projects": {"PRJ": project}}), repo, {})
    assert operational[-1][0]["key"] == "PRJ"
