"""Shared owner-question formatting used by triage and control-side Jira recovery."""
from __future__ import annotations

MAX_OWNER_QUESTION_COMMENT_CHARS = 6000


def _validated_question(question: str, options: list[str], recommended: str) -> tuple[str, list[str], str]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("owner question must be a non-empty string")
    if not isinstance(options, list) or any(not isinstance(option, str) for option in options):
        raise ValueError("owner question options must be a list of strings")
    cleaned_options = [option.strip() for option in options if option.strip()]
    if not cleaned_options:
        raise ValueError("owner question must include at least one option")
    if not isinstance(recommended, str) or not recommended.strip():
        raise ValueError("owner question must include a recommended option")
    return question.strip(), cleaned_options, recommended.strip()


def _render_owner_question(question: str, options: list[str], recommended: str, grouped: bool) -> str:
    lines = [question.strip(), "", "Options:"]
    lines.extend(f"- {option}" for option in options)
    lines.extend(("", f"Recommended option: {recommended}"))
    if grouped:
        lines.append("This ticket is grouped with an existing owner decision.")
    lines.extend(("", "Answer in a Jira comment, then move the ticket to To Do."))

    return "\n".join(lines)


def owner_question_comment(question: str, options: list[str], recommended: str, grouped: bool = False) -> str:
    """Validate and render the complete Jira hand-off within Jira's comment limit."""
    question, options, recommended = _validated_question(question, options, recommended)
    comment = _render_owner_question(question, options, recommended, grouped)
    if len(comment) > MAX_OWNER_QUESTION_COMMENT_CHARS:
        raise ValueError(f"owner question comment exceeds {MAX_OWNER_QUESTION_COMMENT_CHARS} characters")
    return comment
