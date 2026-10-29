"""JAIF-417: after a developer timeout, the next claim uses a larger hard limit."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import claim_project
from aiflow.v4.control.repositories import developer_runs
from aiflow.v4.core.work_limits import retry_hard_minutes, run_outcome_for_error


UTC = timezone.utc


def test_retry_hard_minutes_adds_thirty_per_timeout():
    assert retry_hard_minutes(60, 0) == 60
    assert retry_hard_minutes(60, 1) == 90
    assert retry_hard_minutes(60, 2) == 120


def test_retry_hard_minutes_rejects_invalid_counts():
    with pytest.raises(ValueError):
        retry_hard_minutes(0, 0)
    with pytest.raises(ValueError):
        retry_hard_minutes(60, -1)
    with pytest.raises(ValueError):
        retry_hard_minutes(True, 1)  # type: ignore[arg-type]


def test_timeout_error_kind_is_stored_as_timeout_outcome():
    assert run_outcome_for_error("timeout") == "timeout"
    assert run_outcome_for_error("no changes") == "failed"
    assert run_outcome_for_error("") == "failed"


def _timeout_run(db, ticket: str, *, outcome: str = "timeout", error_kind: str = "timeout") -> None:
    # End outside BACKOFF_SQL so the ticket is claimable; the count still includes the row.
    started = datetime.now(UTC) - timedelta(hours=3)
    db.run(
        "INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
        "error_kind,started_at,ended_at) VALUES (%s,%s,'TESTWP','dev','claude-01','claude','cc',1,"
        "'finished',%s,%s,%s,%s)",
        (str(uuid.uuid4()), ticket, outcome, error_kind, started, started + timedelta(minutes=60)),
    )


def test_timeout_count_includes_outcome_or_error_kind(db):
    _timeout_run(db, "TESTWP-LIMIT-1", outcome="timeout", error_kind="")
    _timeout_run(db, "TESTWP-LIMIT-1", outcome="failed", error_kind="timeout")
    _timeout_run(db, "TESTWP-LIMIT-1", outcome="done", error_kind="")
    assert developer_runs.timeout_count(db, "TESTWP-LIMIT-1") == 2
    assert developer_runs.timeout_count(db, "TESTWP-LIMIT-MISSING") == 0


def _online(client, name: str, account: str) -> dict[str, str]:
    response = join_node(client, name)
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['node_token']}"}
    beat = client.post(
        f"/v1/nodes/{name}/heartbeat",
        json={"slots_total": 2, "workers": [{"account": account, "state": "idle"}]},
        headers=headers,
    )
    assert beat.status_code == 200
    return headers


def test_claim_keeps_project_hard_when_ticket_has_no_timeouts(client, ctx):
    headers = _online(client, "cc", "claude-01")
    add_ticket(ctx.db, "TESTWP-LIMIT-0")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-LIMIT-0", "account": "claude-01"},
                          headers=headers)
    assert claimed.status_code == 200
    assert claimed.json()["project"]["work_minutes"]["hard"] == 60


def test_claim_raises_hard_by_thirty_minutes_per_previous_timeout(client, ctx):
    headers = _online(client, "cc", "claude-01")
    add_ticket(ctx.db, "TESTWP-LIMIT-2")
    _timeout_run(ctx.db, "TESTWP-LIMIT-2")
    _timeout_run(ctx.db, "TESTWP-LIMIT-2", outcome="failed", error_kind="timeout")
    claimed = client.post("/v1/work/claim", json={"ticket": "TESTWP-LIMIT-2", "account": "claude-01"},
                          headers=headers)
    assert claimed.status_code == 200
    minutes = claimed.json()["project"]["work_minutes"]
    assert minutes["target"] == 30 and minutes["warn"] == 45 and minutes["hard"] == 120


class _Project:
    key = "TESTWP"
    hotfix_base = "master"
    environment_items: list = []
    merge: dict = {}
    local_gate = ""

    def work_minutes(self) -> dict[str, int]:
        return {"target": 30, "warn": 45, "hard": 60}


def test_verify_payload_keeps_project_hard_after_developer_timeouts(db):
    _timeout_run(db, "TESTWP-LIMIT-V")
    data = claim_project.payload(_Project(), {"repo": "jooservices/test-aiflow-wp", "base": "develop"},
                                 "cc", "verify", db, "TESTWP-LIMIT-V")
    assert data["work_minutes"]["hard"] == 60
