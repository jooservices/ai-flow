from aiflow.v4.control.worker_candidates import diversify_dev_candidates


def test_dev_candidates_rotate_only_within_the_same_priority_group():
    candidates = [
        {"key": f"P{i % 2}-{i}", "project": f"P{i % 2}", "step": "dev",
         "priority": "Medium", "labels": ["aiflow-ready"]}
        for i in range(8)
    ]
    candidates.extend([
        {"key": "P2-1", "project": "P2", "step": "dev", "priority": "Low", "labels": ["aiflow-ready"]},
        {"key": "P2-2", "project": "P2", "step": "dev", "priority": "Low", "labels": ["aiflow-ready"]},
        {"key": "P0-verify", "project": "P0", "step": "verify", "priority": "Highest", "labels": []},
    ])
    project_priorities = {"P0": 1, "P1": 1, "P2": 2}
    ticket_priorities = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}

    rotations = [diversify_dev_candidates(candidates, f"account-{i}", project_priorities, ticket_priorities)
                 for i in range(64)]

    assert len({tuple(item["key"] for item in ordered[:8]) for ordered in rotations}) > 1
    for ordered in rotations:
        keys = [item["key"] for item in ordered]
        assert sorted(item["key"] for item in ordered) == sorted(item["key"] for item in candidates)
        assert set(keys[:8]) == {item["key"] for item in candidates[:8]}
        assert set(keys[8:10]) == {"P2-1", "P2-2"}
        assert keys[10:] == ["P0-verify"]
    assert rotations[0] == diversify_dev_candidates(candidates, "account-0", project_priorities, ticket_priorities)
