"""JAIF-363: apply a validated split plan to Jira once, with fake Jira."""
from __future__ import annotations

import pytest

from conftest import FakeJira
from aiflow.v4.control.ticket_split_apply import PARENT_LABEL, after_intake_result, apply_plan, child_label
from aiflow.v4.core.ticket_splitting import InvalidSplitPlan


PLAN = [
    {"summary": "First child ticket here", "description": "AC1: First child is testable."},
    {"summary": "Second child ticket here", "description": "AC1: Second child is testable."},
    {"summary": "Third child ticket here", "description": "AC1: Third child is testable."},
]


class TrackingJira(FakeJira):
    def create_issue(self, project, summary, description, issuetype="Story", labels=None, parent="",
                     fix_version="", version_field=""):
        key = super().create_issue(project, summary, description, issuetype=issuetype, labels=labels,
                                   parent=parent, fix_version=fix_version, version_field=version_field)
        self.search_data.append({"key": key, "labels": list(labels or [])})
        return key

    def search(self, jql: str):
        self.search_queries.append(jql)
        needle = None
        if 'labels = "' in jql:
            needle = jql.split('labels = "', 1)[1].split('"', 1)[0]
        if needle is None:
            return iter(self.search_data)
        return iter(row for row in self.search_data if needle in (row.get("labels") or []))


def test_apply_creates_stories_relates_and_blocks_chain():
    jira = TrackingJira()
    result = apply_plan(jira, "TESTWP-40", "TESTWP", PLAN)

    assert result["children"] == ["TESTWP-900", "TESTWP-901", "TESTWP-902"]
    assert [row["issuetype"] for row in jira.created] == ["Story", "Story", "Story"]
    assert [row["summary"] for row in jira.created] == [item["summary"] for item in PLAN]
    assert [row["labels"] for row in jira.created] == [
        [child_label("TESTWP-40", 1)], [child_label("TESTWP-40", 2)], [child_label("TESTWP-40", 3)],
    ]
    assert "aiflow-ready" not in {label for row in jira.created for label in row["labels"]}
    assert ("TESTWP-900", "TESTWP-40", "Relates") in jira.issue_links
    assert ("TESTWP-901", "TESTWP-40", "Relates") in jira.issue_links
    assert ("TESTWP-900", "TESTWP-901", "Blocks") in jira.issue_links
    assert ("TESTWP-901", "TESTWP-902", "Blocks") in jira.issue_links
    assert ("remove", "TESTWP-40", "aiflow-ready") in jira.labels
    assert ("add", "TESTWP-40", PARENT_LABEL) in jira.labels


def test_second_apply_returns_existing_keys_without_duplicates():
    jira = TrackingJira()
    first = apply_plan(jira, "TESTWP-40", "TESTWP", PLAN)
    second = apply_plan(jira, "TESTWP-40", "TESTWP", PLAN)

    assert second["children"] == first["children"]
    assert len(jira.created) == 3
    assert jira.issue_links.count(("TESTWP-900", "TESTWP-40", "Relates")) == 1
    assert jira.issue_links.count(("TESTWP-900", "TESTWP-901", "Blocks")) == 1


def test_invalid_plan_does_not_create_issues():
    jira = TrackingJira()
    with pytest.raises(InvalidSplitPlan):
        apply_plan(jira, "TESTWP-40", "TESTWP", PLAN[:1])
    assert getattr(jira, "created", []) == []
    assert jira.issue_links == []
    assert jira.labels == []


def test_after_intake_result_applies_split_plan_only():
    jira = TrackingJira()

    class Ctx:
        pass

    class Body:
        outcome = "split_plan"
        details = {"split_plan": PLAN}

    ctx = Ctx()
    ctx.jira = jira
    after_intake_result(ctx, {"step": "intake", "ticket_key": "TESTWP-40", "project": "TESTWP"}, Body())
    assert [row["key"] for row in jira.created] == ["TESTWP-900", "TESTWP-901", "TESTWP-902"]

    jira2 = TrackingJira()
    ctx.jira = jira2
    Body.outcome = "refined"
    after_intake_result(ctx, {"step": "intake", "ticket_key": "TESTWP-40", "project": "TESTWP"}, Body())
    assert getattr(jira2, "created", []) == []
