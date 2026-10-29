"""The CI shards of the v4 suite cover every test file exactly once (JAIF-431)."""
from __future__ import annotations

from pathlib import Path

import yaml

from aiflow.v4.core import test_shards as shards

ROOT = Path(__file__).resolve().parents[2]
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_every_test_file_is_in_exactly_one_shard_or_has_its_own_job():
    base = ROOT / "tests" / "v4"
    everything = {p.relative_to(base).as_posix() for p in base.rglob("test_*.py")}
    parts = shards.partition(shards.sharded_files(ROOT), shards.SHARDS)
    in_shards = [p.relative_to(ROOT / "tests" / "v4").as_posix() for part in parts for p in part]
    assert len(in_shards) == len(set(in_shards)), "a test file is in two shards"
    assert set(in_shards) | set(shards.OWN_JOB) == everything
    assert set(in_shards).isdisjoint(shards.OWN_JOB)


def test_no_shard_is_empty_and_the_heaviest_is_not_far_from_the_lightest():
    parts = shards.partition(shards.sharded_files(ROOT), shards.SHARDS)
    sizes = [sum(p.stat().st_size for p in part) for part in parts]
    assert min(sizes) > 0 and max(sizes) <= 1.5 * min(sizes)


def test_ci_runs_every_shard_and_the_required_check_waits_for_all_jobs():
    shard_job = CI["jobs"]["v4-shard"]
    assert shard_job["strategy"]["matrix"]["shard"] == list(range(1, shards.SHARDS + 1))
    assert f"--total {shards.SHARDS}" in str(shard_job["steps"])
    assert 'test -n "$files"' in str(shard_job["steps"])  # a failing selector must stop the shard, not run the whole suite
    required = CI["jobs"]["v4"]
    assert required["name"] == "Test (v4)" and set(required["needs"]) == {"v4-shard", "v4-browser"}
    assert required["if"].startswith("always()")


def test_the_jobs_that_install_the_dependencies_restore_the_uv_cache_before_installing():
    for name in ("docs", "quality", "v4-shard", "v4-browser", "v4-scale"):
        steps = [step.get("name") for step in CI["jobs"][name]["steps"]]
        assert steps.index("Cache uv downloads") < steps.index("Install"), name
