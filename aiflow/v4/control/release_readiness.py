"""Release readiness of a Jira version, computed from the registry on every read (nothing is stored).

A version is ready when every ticket currently linked to it (the Fix versions mirrored by Jira sync into
`ticket_versions`) is:

  Done          registry status `Done`;
  not rework    no `rework` label and its latest verify run did not end in rework;
  verified      its latest finished verify run passed (outcome merged/human, or verdict PASS);
  not blocked   no open Bug labelled `release-qa` blocks it (the Bug's key is in the ticket's `blocked_by`).

An empty version is never ready. Because membership is read live from `ticket_versions`, a ticket the PO adds to
the version mid-way makes it not ready again at the next sync, and removing the only blocker recomputes it.

`evaluate` is pure (tickets, the registry's tickets by key, latest verify run per ticket) so tests can drive it
directly; `project_versions` and `all_versions` load the inputs for the dashboards. This module only reports;
the release gate itself stays in `version_release`.
"""
from __future__ import annotations

from typing import Any, Iterable

from .report import _verify_verdict

REWORK_LABEL = "rework"
RELEASE_QA_LABEL = "release-qa"

CAUSES = ("empty", "not_done", "rework", "not_verified", "blocked_by_bug")


def _reason(cause: str, ticket: dict | None = None, **extra: Any) -> dict[str, Any]:
    t = ticket or {}
    return {"cause": cause, "ticket": t.get("key"), "summary": t.get("summary", ""), **extra}


def blocking_bugs(ticket: dict, tickets_by_key: dict[str, dict]) -> list[str]:
    """Keys of open Bugs labelled release-qa that block this ticket."""
    out = []
    for key in ticket.get("blocked_by") or []:
        bug = tickets_by_key.get(key)
        if (bug and bug.get("type") == "Bug" and bug.get("status") != "Done"
                and RELEASE_QA_LABEL in (bug.get("labels") or [])):
            out.append(key)
    return sorted(out)


def ticket_reasons(ticket: dict, tickets_by_key: dict[str, dict], latest_verify: dict | None) -> list[dict]:
    reasons = []
    done = ticket.get("status") == "Done"
    if not done:
        reasons.append(_reason("not_done", ticket, status=ticket.get("status") or ""))
    rework = (REWORK_LABEL in (ticket.get("labels") or [])
              or str((latest_verify or {}).get("outcome") or "") == "rework")
    if rework:
        reasons.append(_reason("rework", ticket))
    elif done and _verify_verdict(latest_verify) != "PASS":
        reasons.append(_reason("not_verified", ticket))
    for bug in blocking_bugs(ticket, tickets_by_key):
        reasons.append(_reason("blocked_by_bug", ticket, bug=bug))
    return reasons


def evaluate(members: Iterable[dict], tickets_by_key: dict[str, dict],
             latest_verify: dict[str, dict]) -> dict[str, Any]:
    """Readiness of one version from its current member tickets. Returns ready, counts and reasons."""
    members = sorted(members, key=lambda t: t["key"])
    if not members:
        return {"ready": False, "ticket_count": 0, "done_count": 0, "tickets": [], "reasons": [_reason("empty")]}
    reasons = []
    for t in members:
        reasons.extend(ticket_reasons(t, tickets_by_key, latest_verify.get(t["key"])))
    return {"ready": not reasons, "ticket_count": len(members),
            "done_count": sum(1 for t in members if t.get("status") == "Done"),
            "tickets": [t["key"] for t in members], "reasons": reasons}


def _load(db, versions: list[dict]) -> list[dict[str, Any]]:
    if not versions:
        return []
    ids = [v["id"] for v in versions]
    links = db.all("SELECT version_id, ticket_key FROM ticket_versions WHERE version_id = ANY(%s)", (ids,))
    member_keys = {link["ticket_key"] for link in links}
    rows = db.all("SELECT key, status, type, labels, summary, blocked_by FROM tickets WHERE key = ANY(%s)",
                  (list(member_keys),))
    by_key = {r["key"]: r for r in rows}
    blocker_keys = {b for r in rows for b in r["blocked_by"] or []} - set(by_key)
    if blocker_keys:
        by_key.update({r["key"]: r for r in db.all(
            "SELECT key, status, type, labels, summary, blocked_by FROM tickets WHERE key = ANY(%s)",
            (list(blocker_keys),))})
    latest_verify = {r["ticket_key"]: r for r in db.all(
        "SELECT DISTINCT ON (ticket_key) ticket_key, outcome, details FROM runs"
        " WHERE ticket_key = ANY(%s) AND step='verify' AND status='finished'"
        " ORDER BY ticket_key, started_at DESC", (list(member_keys),))} if member_keys else {}
    members: dict[str, list[dict]] = {}
    for link in links:
        if link["ticket_key"] in by_key:
            members.setdefault(link["version_id"], []).append(by_key[link["ticket_key"]])
    out = []
    for v in versions:
        out.append({"project": v["project"], "version_id": v["id"], "version": v["name"],
                    **evaluate(members.get(v["id"], []), by_key, latest_verify)})
    return out


def project_versions(db, project: str) -> list[dict[str, Any]]:
    """Readiness of each unreleased version of one project."""
    return _load(db, db.all("SELECT id, project, name FROM versions WHERE project=%s AND NOT released"
                            " ORDER BY name, id", (project,)))


def all_versions(db, projects: Iterable[str]) -> list[dict[str, Any]]:
    """Readiness of each unreleased version of the configured projects (for /v1/state)."""
    return _load(db, db.all("SELECT id, project, name FROM versions WHERE project = ANY(%s) AND NOT released"
                            " ORDER BY project, name, id", (list(projects),)))
