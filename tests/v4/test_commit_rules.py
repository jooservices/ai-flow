"""Header rules and title normalizer (JAIF-366).

``COMMITLINT`` is a fixture of headers whose result was taken from the real commitlint 19 run with
``.github/commitlint.config.mjs``; the rules module must agree with every row.
"""
from __future__ import annotations

import pytest

from aiflow.v4.core import commit_rules as rules

COMMITLINT = [
    ("feat: Add a thing (JAIF-1)", True),
    ("fix: lower case subject", False),
    ("feat: Role Health weighs a timeout (JAIF-210)", True),
    ("feat: JAIF-137: điều kiện sẵn sàng release", True),
    ("Feat: Wrong type case", False),
    ("feat: Trailing period.", False),
    ("feat: short", False),
    ("feat(scope): Scoped subject is fine", True),
    ("docs: Ends with key (TESTWP-4)", True),
    ("chore: 123 numbers first", True),
    ("feat:no space after colon", False),
    ("feat: ÉCOLE accent first letter", True),
    ("feat: " + "A" * 120, False),
]


@pytest.mark.parametrize("header,accepted", COMMITLINT)
def test_rules_agree_with_the_real_commitlint(header, accepted):
    assert (rules.violations(header) == []) is accepted


def test_pull_request_subject_must_start_with_an_ascii_capital():
    assert rules.violations("feat: Subject here") == []
    assert rules.violations("feat: 123 numbers first", pr_title=True)
    assert rules.violations("feat: Écoles ouvertes", pr_title=True)


def test_message_rules_look_only_at_the_first_line():
    assert rules.message_violations("feat: Add a thing (JAIF-1)\n\nany body, however long " + "x" * 300) == []
    assert rules.message_violations("") != []


@pytest.mark.parametrize("kind,summary,expected", [
    ("feat", "monorepo scaffold.", "feat: Monorepo scaffold (TESTWP-4)"),
    ("fix", "Broken link", "fix: Broken link (TESTWP-4)"),
    ("feat", "Điều kiện sẵn sàng release", "feat: TESTWP-4: Điều kiện sẵn sàng release"),
    ("chore", "  spaced   out  words ... ", "chore: Spaced out words (TESTWP-4)"),
    ("nonsense", "Fine title", "feat: Fine title (TESTWP-4)"),
    ("feat", "", "feat: Change TESTWP-4"),
    ("feat", "...", "feat: Change TESTWP-4"),
])
def test_normalizer_examples(kind, summary, expected):
    assert rules.normalize_title(kind, "TESTWP-4", summary) == expected


@pytest.mark.parametrize("kind", rules.TYPES)
@pytest.mark.parametrize("summary", [
    "x", "Short", "a very long summary " * 20, "Điều kiện " * 30, "word" * 60, "Ends with a colon:", "Ends, ",
    "123 starts with digits and goes on " * 6, "emoji at the end 🚀" * 12,
])
def test_normalized_title_always_passes_every_rule(kind, summary):
    title = rules.normalize_title(kind, "JAIF-366", summary)
    assert rules.violations(title, pr_title=True) == []
    assert len(title) <= rules.TITLE_TARGET


def test_long_titles_are_cut_on_a_word_boundary():
    title = rules.normalize_title("feat", "JAIF-1", "alpha beta gamma delta " * 10)
    assert title.endswith(" (JAIF-1)")
    assert not title.removesuffix(" (JAIF-1)").endswith(("alph", "bet", "gamm", "delt"))


def test_a_cut_title_does_not_end_on_a_dangling_word():
    title = rules.normalize_title("feat", "JAIF-368", "A missing tool or environment item is solved by the flow and never sent to the owner")
    assert title == "feat: A missing tool or environment item is solved by the flow and never sent (JAIF-368)"


@pytest.mark.parametrize("summary", [
    "Apply the recommendation of an unanswered owner question after its deadline and then do more things here",
    "Audit the 236 control API routes against the company HTTP standard and propose each target design now",
])
def test_cut_titles_never_end_on_a_determiner_or_pronoun(summary):
    title = rules.normalize_title("feat", "K-1", summary)
    assert rules.violations(title, pr_title=True) == []
    last_word = title.removesuffix(" (K-1)").split(" ")[-1].lower()
    assert last_word not in {"its", "their", "this", "each", "every", "any", "some", "the", "a", "and", "to"}
