from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiflow.v4.control.app_helpers import (
    _flow_bug_description,
    _flow_bug_is_closed,
    _flow_bug_metadata,
    _flow_bug_repeat_comment,
)
from aiflow.v4.control.incidents import _candidate_run, _candidate_stats, _normalize_failure_sample, flow_error_pattern


def test_failure_signature_uses_first_normalized_line_and_binds_outcome_across_steps():
    first = flow_error_pattern("infra", "git checkout failed for TESTWP-12 at /workspace/app/file.py 42\nignored",
                               "dev", "failed")
    second = flow_error_pattern("infra", "git checkout failed for TESTWP-99 at /workspace/app/file.py 77\nother",
                                "dev", "failed")

    assert first[0] == second[0]
    assert first[1] != second[1]
    assert first[1] == "git checkout failed for TESTWP-12 at /workspace/app/file.py 42"
    assert _normalize_failure_sample("worker error: checkout failed in src/file.py 12")[1]
    assert flow_error_pattern("infra", "git checkout failed for TESTWP-12 at /workspace/app/file.py 42",
                              "verify", "failed")[0] == first[0]
    assert flow_error_pattern("infra", "git checkout failed for TESTWP-12 at /workspace/app/file.py 42",
                              "dev", "timeout")[0] != first[0]


def test_candidate_run_and_stats_keep_accounts_cost_and_three_samples():
    now = datetime.now(timezone.utc)
    rows = [_candidate_run({"id": str(index), "ticket_key": "OTHER-1", "project": "OTHER", "step": "verify",
                            "outcome": "failed", "error_kind": "infra", "sample": f"sample {index}",
                            "node": "node1", "account_id": "codex-01", "provider": "codex",
                            "release_version": "v4.2", "started_at": now + timedelta(seconds=index),
                            "tokens_in": 2, "tokens_out": 3, "cost_usd": 0.25}) for index in range(3)]
    stats = _candidate_stats({"signature": "sig", "pattern": "sample", "error_kind": "infra", "step": "verify",
                             "outcome": "failed", "runs": {run["id"]: run for run in rows}})

    assert stats["window_count"] == 3 and stats["tickets"] == ["OTHER-1"]
    assert stats["provider_accounts"] == ["codex/codex-01"]
    assert stats["tokens_used"] == 15 and stats["cost_wasted"] == 0.75
    assert len(stats["sample_messages"]) == 3 and stats["creation_eligible"]


def test_flow_bug_metadata_merges_totals_and_breaker_tickets():
    seen = datetime.now(timezone.utc)
    run = {"id": "r4", "tokens": 12, "cost_usd": 0.4}
    candidate = {"runs": [run], "tickets": ["OTHER-2"], "steps": ["dev"], "providers": ["codex"],
                 "accounts": ["codex-01"], "first_seen_at": seen, "last_seen_at": seen,
                 "sample_messages": ["message"], "step": "dev"}
    tracker = {"run_ids": ["r1"], "repeat_count": 2, "occurrence_count": 3,
               "first_seen_at": seen - timedelta(hours=1), "last_seen_at": seen - timedelta(minutes=1),
               "tokens_wasted": 30, "cost_wasted": 1.0}

    metadata = _flow_bug_metadata(candidate, tracker, [run], True, 1)

    assert metadata["occurrence_count"] == 4 and metadata["repeat_count"] == 3
    assert metadata["tokens_wasted"] == 42 and metadata["cost_wasted"] == 1.4
    assert metadata["breaker_active"] and metadata["blocked_step"] == "dev"
    assert metadata["regression_count"] == 1


def test_bug_text_carries_repeats_regressions_and_breaker_state():
    now = datetime.now(timezone.utc)
    run = {"id": "run-1", "ticket": "OTHER-1", "step": "dev", "provider": "codex", "account": "codex-01",
           "node": "node1", "release": "v4.2", "tokens": 7}
    candidate = {"signature": "sig", "pattern": "checkout failed", "error_kind": "infra", "runs": [run],
                 "sample_messages": ["one", "two", "three"], "tickets": ["OTHER-1"], "steps": ["dev"],
                 "providers": ["codex"], "accounts": ["codex-01"], "provider_accounts": ["codex/codex-01"],
                 "first_seen_at": now, "last_seen_at": now,
                 "tokens_used": 7, "cost_wasted": 0.5, "nodes": ["node1"], "releases": ["v4.2"]}

    description = _flow_bug_description(candidate, 10, True)
    comment = _flow_bug_repeat_comment(candidate, [run], 11, 20, 0.9, 1, True)

    assert "Count: 10" in description and "Three sample messages:" in description
    assert "Provider accounts: codex/codex-01" in description
    assert "Circuit breaker active" in description and "Cost wasted: $0.500000" in description
    assert "Count: 11" in comment and "Regression count: 1" in comment
    assert "Circuit breaker active" in comment and "run-1" in comment


def test_terminal_bug_statuses_are_case_insensitive():
    assert _flow_bug_is_closed("Done") and _flow_bug_is_closed("RESOLVED")
    assert not _flow_bug_is_closed("To Do")


def test_flow_bug_signature_members_expire_after_ninety_days(ctx):
    from aiflow.v4.control.repositories import flow_bugs as flow_bug_repository

    signature = "retention-signature"
    ctx.db.run("INSERT INTO flow_bug_tracker (signature,ticket_key) VALUES (%s,'OPS-1')", (signature,))
    ctx.db.run("INSERT INTO flow_bug_signature_members"
               " (signature,kind,value,first_seen_at,last_seen_at,occurrences)"
               " VALUES (%s,'ticket','OPS-OLD',now()-interval '91 days',now()-interval '91 days',1),"
               " (%s,'ticket','OPS-NEW',now()-interval '2 days',now()-interval '2 days',2)",
               (signature, signature))
    ctx.db.run("INSERT INTO flow_bug_blocked_tickets (signature,ticket_key) VALUES (%s,'OPS-OLD'),(%s,'OPS-NEW')",
               (signature, signature))
    ctx.db.run("INSERT INTO flow_bug_sample_messages (signature,position,message) VALUES (%s,1,'retained')",
               (signature,))

    flow_bug_repository.prune_expired_members(ctx.db)

    members = ctx.db.all("SELECT value FROM flow_bug_signature_members WHERE signature=%s ORDER BY value",
                         (signature,))
    blocked = ctx.db.all("SELECT ticket_key FROM flow_bug_blocked_tickets WHERE signature=%s ORDER BY ticket_key",
                         (signature,))
    samples = ctx.db.all("SELECT message FROM flow_bug_sample_messages WHERE signature=%s", (signature,))
    assert members == [{"value": "OPS-NEW"}]
    assert blocked == [{"ticket_key": "OPS-NEW"}]
    assert samples == [{"message": "retained"}]
