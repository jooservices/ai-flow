"""Short study cases of known review defects, given to a developer run by area and task type (JAIF-584).

The store is ``prompts/v4/study-cases.json``. A case is delivered only when it names a cause, and the lessons of one run
stay within ``TOKEN_CAP`` tokens, estimated at ``CHARS_PER_TOKEN`` characters per token. Selection, the ledger and the
repeat check are plain scripts, so they cost no AI tokens. The ledger in ``<srv_root>/logs`` records which cases each
run was given and which of them came back as a finding again, so a repeated defect is visible.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE = Path(__file__).resolve().parents[3] / "prompts" / "v4" / "study-cases.json"
TOKEN_CAP = 600
CHARS_PER_TOKEN = 4
LEDGER = "study-case-ledger.jsonl"
REQUIRED_FIELDS = ("id", "title", "defect", "cause", "fix", "signature")
LESSON_HEADER = "\nLessons from earlier faults (study cases; check the ticket and the code if they conflict):\n"
PATH_PATTERN = re.compile(r"\b(?:aiflow|prompts|docs|deploy|tests)/[\w./-]+")


def load_cases(path: Path = STORE) -> list[dict[str, Any]]:
    """Read the store and keep the cases that have every field, including the cause."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    return [case for case in cases if all(case.get(field) for field in REQUIRED_FIELDS)]


def task_type_of(issue_type: str | None) -> str:
    """Map a Jira issue type to the task type that study cases are filed under."""
    return "bug" if (issue_type or "").strip().lower() == "bug" else "change"


def ticket_paths(text: str) -> list[str]:
    """Repository paths named in the ticket text, in the order they first appear."""
    return list(dict.fromkeys(PATH_PATTERN.findall(text or "")))


def relevance(case: dict[str, Any], paths: list[str], task_type: str) -> int:
    """Number of the case's areas that the ticket paths touch, 0 for a general case, -1 when it does not apply."""
    if case.get("task_types") and task_type not in case["task_types"]:
        return -1
    areas = case.get("areas") or []
    if not areas:
        return 0
    touched = sum(any(path.startswith(area) for path in paths) for area in areas)
    return touched or -1


def format_case(case: dict[str, Any]) -> str:
    """One short line per case: the defect, its cause and how to avoid it."""
    return f"- [{case['id']}] {case['title']}: {case['defect']} Cause: {case['cause']} Avoid: {case['fix']}"


def select_cases(cases: list[dict[str, Any]], paths: list[str], task_type: str,
                 cap_tokens: int = TOKEN_CAP) -> list[dict[str, Any]]:
    """Relevant cases, most specific first, skipping any case that would take the lessons past the token cap."""
    budget = cap_tokens * CHARS_PER_TOKEN - len(LESSON_HEADER)
    ranked = sorted(((relevance(case, paths, task_type), case) for case in cases),
                    key=lambda pair: (-pair[0], pair[1]["id"]))
    chosen, used = [], 0
    for score, case in ranked:
        size = len(format_case(case)) + 1
        if score >= 0 and used + size <= budget:
            chosen.append(case)
            used += size
    return chosen


def lessons_block(cases: list[dict[str, Any]]) -> str:
    """The lessons section of a developer prompt; empty when no case applies."""
    if not cases:
        return ""
    return LESSON_HEADER + "\n".join(format_case(case) for case in cases) + "\n"


def read_ledger(srv_root: Path) -> list[dict[str, Any]]:
    """Every ledger row written so far, oldest first."""
    path = Path(srv_root) / "logs" / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repeated_case_ids(ticket_key: str, ledger: list[dict[str, Any]], findings: str,
                      cases: list[dict[str, Any]]) -> list[str]:
    """Cases given earlier to this ticket whose signature appears again in the reviewer's findings."""
    given = {case_id for row in ledger if row.get("ticket") == ticket_key for case_id in row.get("given", [])}
    text = (findings or "").lower()
    return sorted(case["id"] for case in cases
                  if case["id"] in given and any(term in text for term in case["signature"]))


def append_ledger_row(srv_root: Path, row: dict[str, Any]) -> None:
    """Append one row to the ledger, creating the logs directory when it is missing."""
    path = Path(srv_root) / "logs" / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(row, sort_keys=True) + "\n")


def deliver_lessons(srv_root: Path, ticket: dict[str, Any], run_id: str, findings: str = "",
                    cases: list[dict[str, Any]] | None = None) -> str:
    """Select the lessons for one developer run, record them in the ledger and return the prompt section."""
    cases = load_cases() if cases is None else cases
    ledger = read_ledger(srv_root)
    paths = ticket_paths(f"{ticket.get('summary', '')}\n{ticket.get('description') or ''}")
    chosen = select_cases(cases, paths, task_type_of(ticket.get("type")))
    repeated = repeated_case_ids(ticket["key"], ledger, findings, cases)
    append_ledger_row(srv_root, {"ticket": ticket["key"], "run_id": run_id, "given": [c["id"] for c in chosen],
                                 "repeated": repeated, "at": datetime.now(timezone.utc).isoformat()})
    return lessons_block(chosen)
