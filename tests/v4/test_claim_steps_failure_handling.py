"""Tests for error handling during version release claim initialization."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiflow.v4.control.features.version_release import claim_steps
from tests.v4.test_version_release_claim_parity import FakeContext, LeaseRegistry, RecordingDb, RecordingEvents


def test_start_release_run_failure_releases_claimed_lease_and_propagates_error() -> None:
    """Verify that an unexpected failure during release claim initialization releases the lease and re-raises."""
    calls: list[str] = []
    leases = LeaseRegistry({}, calls)
    ctx = FakeContext(
        db=RecordingDb({}, calls),
        leases=leases,
        events=RecordingEvents(calls),
        settings=SimpleNamespace(lease_ms=60000),
    )
    project = SimpleNamespace(key="TESTWP", repo="owner/project")
    version = {"id": "101", "name": "Core 1.0"}
    account = SimpleNamespace(id="release-1", provider="claude")
    expected_key = claim_steps.version_release.version_identity(project.key, version["id"])

    def failing_github_gateway(_ctx: object, _repo: str) -> None:
        raise RuntimeError("simulated gateway failure")

    with pytest.raises(RuntimeError, match="simulated gateway failure"):
        claim_steps.start_release_run(
            ctx, project, version, {}, account, "node-1", 6, failing_github_gateway
        )

    claim_calls = [item for item in calls if item.startswith("lease:claim:")]
    release_calls = [item for item in calls if item.startswith("lease:release:")]
    assert len(claim_calls) == 1
    assert len(release_calls) == 1
    assert claim_calls[0].startswith(f"lease:claim:{expected_key}:")
    run_id = claim_calls[0].removeprefix(f"lease:claim:{expected_key}:").split(":")[0]
    assert release_calls[0] == f"lease:release:{expected_key}:{run_id}:19"
