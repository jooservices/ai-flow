from aiflow.v4.core import delivery_metrics as dm


def _pr(number, created, merged, branch, headlines=(), files=()):
    return {"number": number, "createdAt": created, "mergedAt": merged, "headRefName": branch,
            "commits": [{"messageHeadline": h} for h in headlines], "files": [{"path": f} for f in files]}


def test_metrics_count_lead_time_branch_updates_ci_attempts_and_hot_files():
    prs = [_pr(1, "2026-10-01T00:00:00Z", "2026-10-01T02:00:00Z", "a", ["fix: x", "Merge branch 'develop' into a"],
               ["aiflow/v4/control/api.py"]),
           _pr(2, "2026-10-01T00:00:00Z", "2026-10-01T06:00:00Z", "b", ["fix: y"], ["docs/x.md"])]
    runs = [{"head_branch": "a", "run_attempt": 1}, {"head_branch": "a", "run_attempt": 2},
            {"head_branch": "b", "run_attempt": 1}]

    metrics = dm.compute(prs, runs)

    assert metrics["pull_requests"] == 2
    assert metrics["lead_time_hours"]["median"] == 4.0
    assert metrics["prs_with_branch_update_commits"] == 1
    assert metrics["branch_update_commits_per_pr"] == 0.5
    assert metrics["ci_attempts_per_pr"] == 2.0
    assert metrics["hot_file_prs"]["aiflow/v4/control/api.py"] == 1
    assert metrics["hot_file_share"]["aiflow/v4/control/api.py"] == 0.5


def test_metrics_of_no_pull_requests_are_zero_and_render_as_a_table():
    metrics = dm.compute([], [])

    assert metrics["pull_requests"] == 0 and metrics["ci_attempts_per_pr"] == 0.0
    assert "| Lead time, median (hours) | 0.0 |" in dm.render(metrics, "2026-10-30")
