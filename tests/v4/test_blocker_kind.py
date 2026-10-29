"""A tool reported missing in free text is a machine problem and never reaches the owner (JAIF-368)."""
from __future__ import annotations

import pytest

from aiflow.v4.control import escalations as esc
from aiflow.v4.core import blocker_kind
from aiflow.v4.core.toolchains import NODE_TOOLS, known_requirements

MISSING_TEXTS = [
    ("uv is absent from the sandbox, so the required test command cannot run. Provide Python 3.12, uv and the project "
     "dependencies.", "uv"),
    ("BLOCKED: `uv: command not found`; the shared runtime is Python 3.14", "uv"),
    ("The required command `uv run --frozen pytest tests/v4 -q` cannot run because uv is missing in this environment.", "uv"),
    ("bash: composer: command not found", "composer"),
    ("php is not installed on this machine", "php"),
    ("There is no node binary on this machine, so playwright cannot start", "node"),
    ("Python 3 is not available, so the scripts cannot run", "python3"),
]
NOT_MISSING_TEXTS = [
    "The ticket conflicts with AC2: should the endpoint return 404 or 410?",
    "Should we publish the release tag to production now?",
    "The uvicorn server started and the missing slash in the route was fixed",
    "Which of the two designs do you prefer for the new filter?",
    "git history was rewritten upstream; confirm that the new base is correct",
]


class NoDB:
    def all(self, *args, **kwargs):
        return []

    def one(self, *args, **kwargs):
        return None


@pytest.mark.parametrize("text,tool", MISSING_TEXTS)
def test_free_text_missing_tool_is_recognized(text, tool):
    assert tool in blocker_kind.missing_tools(text)


@pytest.mark.parametrize("text", NOT_MISSING_TEXTS)
def test_other_sentences_are_not_tools(text):
    assert blocker_kind.missing_tools(text) == []


@pytest.mark.parametrize("text,tool", MISSING_TEXTS)
def test_the_classifier_sends_a_missing_tool_to_the_machine_path(text, tool):
    c = esc.classify(NoDB(), text, "JAIF")
    assert c.cls == esc.MISSING and not c.reaches_person
    assert tool in c.requirements and c.environment in c.requirements


@pytest.mark.parametrize("text,expected", [
    ("The ticket conflicts with AC2: should the endpoint return 404 or 410?", esc.BUSINESS),
    ("Should we publish the release tag to production now?", esc.IRREVERSIBLE),
])
def test_real_owner_questions_still_reach_the_owner(text, expected):
    assert esc.classify(NoDB(), text, "JAIF").cls == expected


def test_uv_and_python_are_in_the_catalog_the_nodes_can_install():
    assert {"uv", "python3"} <= set(known_requirements())
    assert "uv" in NODE_TOOLS
