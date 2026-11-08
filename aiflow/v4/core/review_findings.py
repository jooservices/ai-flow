"""Stable finding IDs and review state carried between verification rounds."""
from __future__ import annotations

import re
from typing import Any


def _section(text: str, heading: str) -> str | None:
    match = re.search(rf"^{re.escape(heading)}:[ \t]*\n((?:[ \t]*[-*].*(?:\n|$))*)", text or "", re.M)
    return match.group(1) if match else None


def legacy_findings(text: str, require_section: bool = False) -> list[str]:
    """Read old bullet-only FINDINGS output and remove list markers while retaining its wording.

    A reviewer's answer needs its FINDINGS section: bullets elsewhere (for example the deferred checks of a PASS) are not
    defects. A stored rework summary is already only the findings, so it is read without the heading.
    """
    body = _section(text, "FINDINGS")
    if body is None:
        if require_section:
            return []
        body = text
    items = []
    for line in body.splitlines():
        match = re.match(r"^[ \t]*[-*][ \t]*(.+)$", line)
        if not match:
            continue
        value = re.sub(r"^\d+[.)][ \t]*", "", match.group(1)).strip()
        value = re.sub(r"^F\d+[:.)][ \t]*", "", value)
        if value:
            items.append(value)
    return items


def format_findings(history: list[dict[str, Any]]) -> str:
    """Build the numbered developer list from unresolved and new findings."""
    active = [item for item in history if item.get("status") in {"not_fixed", "new"}]
    return "\n".join(f"- {index}. {item['id']}: {item['wording']}" for index, item in enumerate(active, 1))


def format_review_history(history: list[dict[str, Any]]) -> str:
    """Render all known findings with their latest status for the next reviewer."""
    if not history:
        return "No earlier review findings."
    return "\n".join(f"- {item['id']} [{str(item['status']).replace('_', ' ').upper()}]: {item['wording']}"
                     for item in history)


def _legacy_new_items(text: str, history: list[dict[str, Any]]) -> list[tuple[str, str]]:
    reported = legacy_findings(text, require_section=True)
    for item in history:
        item["status"] = "not_fixed"
    return [("", value) for value in reported
            if not any(value == item["wording"] or value.startswith(item["id"] + ":") for item in history)]


def _structured_findings(block: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    states: dict[str, str] = {}
    new_items: list[tuple[str, str]] = []
    for line in block.splitlines():
        old = re.match(r"^[ \t]*[-*][ \t]*(F\d+)[ \t]+\[(FIXED|NOT FIXED)\](?:[ \t]*:.*)?$", line, re.I)
        if old:
            states[old.group(1).upper()] = "fixed" if old.group(2).upper() == "FIXED" else "not_fixed"
            continue
        new = re.match(r"^[ \t]*[-*][ \t]*(?:(F\d+)[ \t]+)?(?:\[NEW\]|NEW:)[ \t]*(.+)$", line, re.I)
        if new:
            new_items.append((str(new.group(1) or "").upper(), new.group(2).strip()))
    return states, new_items


def _append_new_findings(history: list[dict[str, Any]], new_items: list[tuple[str, str]],
                         round_number: int) -> None:
    next_id = max((int(match.group(1)) for item in history
                   if (match := re.fullmatch(r"F(\d+)", item["id"]))), default=0) + 1
    known = {item["id"] for item in history}
    for requested_id, wording in new_items:
        if not wording:
            continue
        while f"F{next_id}" in known:
            next_id += 1
        finding_id = requested_id if requested_id and requested_id not in known else f"F{next_id}"
        known.add(finding_id)
        if finding_id == f"F{next_id}":
            next_id += 1
        history.append({"id": finding_id, "wording": wording, "status": "new", "first_round": max(1, round_number)})


def update_review_findings(text: str, previous: list[dict[str, Any]], round_number: int
                           ) -> tuple[list[dict[str, Any]], str, int]:
    """Apply review statuses, preserve old wording, and return history, active list and new-finding count."""
    history = [{"id": str(item["id"]), "wording": str(item["wording"]),
                "status": str(item.get("status") or "not_fixed"),
                "first_round": int(item.get("first_round") or 1)}
               for item in previous if item.get("id") and item.get("wording")]
    block = _section(text, "REVIEW FINDINGS")
    new_items: list[tuple[str, str]]
    if block is None:
        new_items = _legacy_new_items(text, history)
    else:
        states, new_items = _structured_findings(block)
        for item in history:
            item["status"] = states.get(item["id"], "not_fixed")
    _append_new_findings(history, new_items, round_number)
    current_round = max(1, int(round_number))
    new_count = sum(item["status"] == "new" for item in history) if current_round > 1 else 0
    return history, format_findings(history), new_count


def history_from_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recover the latest finding state, or reconstruct it from pre-JAIF-351 rework runs."""
    for run in reversed(runs):
        stored = _stored_history(run)
        if stored is not None:
            return stored
    return _legacy_history(runs)


def _stored_history(run: dict[str, Any]) -> list[dict[str, Any]] | None:
    details = run.get("details") or {}
    stored = details.get("review_findings")
    if details.get("verdict") not in {"PASS", "FAIL"} or not isinstance(stored, list):
        return None
    return [dict(item) for item in stored if isinstance(item, dict) and item.get("id") and item.get("wording")]


def _legacy_history(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    round_number = 0
    for run in runs:
        details = run.get("details") or {}
        verdict = str(details.get("verdict") or "").upper()
        if verdict in {"PASS", "FAIL"}:
            round_number += 1
        if run.get("outcome") == "rework" and details.get("findings"):
            # a stored rework summary holds only the findings: give it the heading a reviewer's answer would carry
            history, _, _ = update_review_findings("FINDINGS:\n" + str(details["findings"]), history, max(1, round_number))
    return history


def review_rounds_completed(runs: list[dict[str, Any]]) -> int:
    """Count completed AI reviews, excluding verification runs that never received a review response."""
    return sum(1 for run in runs if (run.get("details") or {}).get("verdict") in {"PASS", "FAIL"})


def first_reviewer(runs: list[dict[str, Any]]) -> str:
    """Return the account from the earliest run that received a completed review verdict."""
    return next((str(run.get("account_id") or "") for run in runs
                 if (run.get("details") or {}).get("verdict") in {"PASS", "FAIL"}), "")


