"""Characterization of the developer prompt before the read-before-write brief and study cases (JAIF-584).

These tests pass on the code as it was before the change and must keep passing: a run that gets no lessons sees the
same developer prompt as before.
"""
from __future__ import annotations

from types import SimpleNamespace

from aiflow.v4.core import study_cases
from aiflow.v4.worker import run_steps


def _claim(description: str = "Fix the thing.", findings: str = "") -> SimpleNamespace:
    ticket = {"key": "PRJ-1", "project": "PRJ", "summary": "Check prompt", "description": description}
    project = {"key": "PRJ", "repo": "org/repo", "base": "develop", "checks": ["pytest -q"], "environment": []}
    return SimpleNamespace(project=project, ticket=ticket, findings=findings, triage_answer="", triage_question="",
                           related=[], machine_tools={}, context_pack={}, run_id="run-1")


def test_developer_prompt_keeps_its_sections_when_no_lessons_are_added():
    prompt = run_steps.build_prompt(_claim(), "PRJ-1-work", env={})
    assert prompt.startswith("You are the developer of Jira ticket PRJ-1 (PRJ): \"Check prompt\".")
    assert "Ticket description:\n---\nFix the thing.\n---" in prompt
    assert "1. Requirements ledger." in prompt
    assert "Test order and time budget:" in prompt
    assert study_cases.LESSON_HEADER not in prompt
    assert "Knowledge context pack" not in prompt


def test_developer_prompt_is_the_same_for_the_same_claim():
    assert run_steps.build_prompt(_claim(), "PRJ-1-work", env={}) == run_steps.build_prompt(_claim(), "PRJ-1-work",
                                                                                          env={})
