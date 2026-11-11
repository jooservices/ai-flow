"""Regression guard for duplicate blocks involving container worker modules."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from aiflow.v4.core import duplicate_blocks as guard

ROOT = Path(__file__).resolve().parents[2]
_WORKER_PREFIX = "aiflow/v4/worker/"


def _is_container_path(path: str) -> bool:
    if not path.startswith(_WORKER_PREFIX):
        return False
    name = path.removeprefix(_WORKER_PREFIX)
    return "/" not in name and (name == "containers.py" or name.startswith("container_") and name.endswith(".py"))


def _container_findings(findings: Sequence[guard.Finding]) -> tuple[guard.Finding, ...]:
    return tuple(finding for finding in findings
                 if any(_is_container_path(path) for path, _line in finding.occurrences))


def _format_findings(findings: Sequence[guard.Finding]) -> str:
    details = [f"{finding.fingerprint}: " + ", ".join(
        f"{path}:{line}" for path, line in finding.occurrences
    ) for finding in findings]
    return "Container duplicate blocks found:\n" + "\n".join(details)


def test_container_duplicate_guard_scans_the_repository_tree():
    violations = _container_findings(guard.scan_tree(ROOT))
    assert not violations, _format_findings(violations)


def test_container_path_filter_ignores_empty_unrelated_and_lookalike_findings():
    unrelated = guard.Finding("a" * 16, (("aiflow/v4/control/api.py", 5), ("aiflow/v4/node/agent.py", 9)))
    lookalikes = (
        "aiflow/v4/worker/containers_extra.py",
        "aiflow/v4/worker/container.py",
        "aiflow/v4/worker/nested/container_client.py",
        "aiflow/v4/workerish/container_client.py",
        "aiflow/v4/control/container_client.py",
    )
    findings = (unrelated, *(guard.Finding(f"{index:016x}", ((path, 1),))
                             for index, path in enumerate(lookalikes, start=1)))

    assert _container_findings(()) == ()
    assert _container_findings(findings) == ()


def test_container_path_filter_matches_first_and_later_occurrences():
    first = guard.Finding("b" * 16, (
        ("aiflow/v4/worker/containers.py", 3),
        ("aiflow/v4/control/api.py", 8),
    ))
    later = guard.Finding("c" * 16, (
        ("aiflow/v4/control/api.py", 13),
        ("aiflow/v4/worker/container_policy.py", 21),
    ))
    dynamic = guard.Finding("d" * 16, (("aiflow/v4/worker/container_other.py", 34),))

    assert _container_findings((first, later, dynamic)) == (first, later, dynamic)


def test_container_duplicate_diagnostics_show_fingerprint_and_all_locations():
    finding = guard.Finding("e" * 16, (
        ("aiflow/v4/control/api.py", 4),
        ("aiflow/v4/worker/container_client.py", 12),
        ("aiflow/v4/worker/containers.py", 27),
    ))

    message = _format_findings(_container_findings((finding,)))

    assert "e" * 16 in message
    assert "aiflow/v4/control/api.py:4" in message
    assert "aiflow/v4/worker/container_client.py:12" in message
    assert "aiflow/v4/worker/containers.py:27" in message
