"""Release progress of each Jira version, read-only, for the Command Center and Business screens.

Per version (unreleased first, then the most recent released ones):

  progress   Done / total tickets and the open tickets, from the Fix versions mirrored into `ticket_versions`.
  late       a ticket is added late (`added_late`, with `added_at`) when it was attached to the version, per the
             Jira change history kept in `ticket_version_added`, after the first ticket of the version moved to
             In Progress (`tickets.first_in_progress_at`). Late tickets count in the total like any other.
  reasons    why the version is not ready: the conditions of `release_readiness` plus a running or failed release
             check taken from `version_releases` (`release_running`, `release_failed`). Empty once released.
  release    the state of the release flow of `version_release`: status, tracker, tag, GitHub Release link and the
             steps check, changelog, PR, merge, tag and release with their run ID and node. Steps come from the
             write_audit and events rows that flow already writes, so nothing is stored here.

Nothing is written: `all_versions` and `project_versions` only read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from . import release_readiness

RELEASED_SHOWN = 10  # most recent released versions listed per project
STEPS = ("check", "changelog", "pr", "merge", "tag", "release")


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def mark_late(members: list[dict], added: dict[str, datetime | None], started: datetime | None) -> list[dict]:
    """Members with `added_late` and `added_at`; `started` is the first In Progress move of any member."""
    out = []
    for t in sorted(members, key=lambda m: m["key"]):
        at = added.get(t["key"])
        out.append({"key": t["key"], "summary": t.get("summary", ""), "status": t.get("status") or "",
                    "added_at": _iso(at), "added_late": bool(started and at and at > started)})
    return out


def _step(name: str, state: str = "pending", **extra: Any) -> dict[str, Any]:
    return {"step": name, "state": state, "run_id": "", "node": "", "detail": "", "at": None, **extra}


def release_steps(db, tracker: str, status: str) -> list[dict[str, Any]]:
    """The release flow steps for one tracker ticket: state is done | failed | running | pending."""
    audits = {}
    for row in db.all("SELECT id, ts, action, run_id, result, payload FROM write_audit WHERE ticket=%s"
                      " AND action = ANY(%s) ORDER BY id", (tracker, ["github-release-files", "github-merge",
                                                                      "github-tag", "github-release"])):
        audits[row["action"]] = row  # the latest of each action wins
    checks = db.all("SELECT ts, run_id, outcome, reason FROM events WHERE ticket=%s AND type='version_release.check'"
                    " ORDER BY id DESC LIMIT 1", (tracker,))
    run_ids = {r["run_id"] for r in audits.values() if r["run_id"]} | {c["run_id"] for c in checks if c["run_id"]}
    nodes = {str(r["id"]): r["node"] for r in db.all(
        "SELECT id, node FROM runs WHERE id::text = ANY(%s)", (list(run_ids),))} if run_ids else {}

    def fill(name: str, state: str, row: dict | None, detail: str = "") -> dict[str, Any]:
        run_id = (row or {}).get("run_id") or ""
        return _step(name, state, run_id=run_id, node=nodes.get(run_id, ""), detail=detail,
                     at=_iso((row or {}).get("ts")))

    files, merge = audits.get("github-release-files"), audits.get("github-merge")
    tag, release = audits.get("github-tag"), audits.get("github-release")
    payload = (merge or {}).get("payload") or {}
    steps = []
    check = checks[0] if checks else None
    if files or merge:
        steps.append(fill("check", "done", files or merge, "release checks passed"))
    elif check and check["outcome"] in ("failed", "blocked", "stale"):
        steps.append(fill("check", "failed", check, check["reason"][:300]))
    else:
        steps.append(_step("check", "running" if status == "checking" else "pending"))
    steps.append(fill("changelog", "done", files, ", ".join((files or {}).get("payload", {}).get("files") or []))
                 if files else _step("changelog"))
    pr_label = f"#{payload['pr_number']}" if payload.get("pr_number") else ""
    steps.append({**fill("pr", "done", merge, pr_label), "url": payload.get("pr_url", "")} if merge else _step("pr"))
    steps.append(fill("merge", "done", merge, (merge["result"] or "")[:12]) if merge else _step("merge"))
    steps.append(fill("tag", "done", tag, (tag["payload"] or {}).get("tag", "")) if tag else _step("tag"))
    steps.append({**fill("release", "done", release, ""), "url": release["result"]
                  if release and str(release["result"]).startswith("http") else ""} if release else _step("release"))
    if status == "failed":
        # the first step that has not finished is where the flow stopped
        for s in steps:
            if s["state"] == "pending":
                s["state"] = "failed"
                break
    elif status in ("checking", "pending", "blocked"):
        for s in steps:
            if s["state"] == "pending":
                if status == "checking":
                    s["state"] = "running"
                break
    return steps


def _release(db, row: dict | None, version: dict) -> dict[str, Any]:
    if not row:
        return {"status": "released" if version["released"] else "none", "tracker": "", "tag": "",
                "github_release_url": "", "detail": "", "steps": []}
    status = "released" if version["released"] and row["status"] == "released" else row["status"]
    return {"status": status, "tracker": row["tracker_key"], "tag": row["tag"],
            "github_release_url": row["github_release_url"], "detail": row["gate_detail"],
            "steps": release_steps(db, row["tracker_key"], row["status"]) if row["tracker_key"] else []}


def _extra_reasons(version: dict, release: dict) -> list[dict[str, Any]]:
    if version["released"]:
        return []
    if release["status"] == "checking":
        return [{"cause": "release_running", "ticket": None, "summary": "", "detail": release["detail"]}]
    if release["status"] == "failed":
        return [{"cause": "release_failed", "ticket": None, "summary": "", "detail": release["detail"]}]
    return []


def _load(db, versions: list[dict]) -> list[dict[str, Any]]:
    if not versions:
        return []
    ids = [v["id"] for v in versions]
    links = db.all("SELECT version_id, ticket_key FROM ticket_versions WHERE version_id = ANY(%s)", (ids,))
    keys = list({link["ticket_key"] for link in links})
    rows = db.all("SELECT key, status, type, labels, summary, blocked_by, first_in_progress_at FROM tickets"
                  " WHERE key = ANY(%s)", (keys,)) if keys else []
    by_key = {r["key"]: r for r in rows}
    blockers = {b for r in rows for b in r["blocked_by"] or []} - set(by_key)
    if blockers:
        by_key.update({r["key"]: r for r in db.all(
            "SELECT key, status, type, labels, summary, blocked_by FROM tickets WHERE key = ANY(%s)",
            (list(blockers),))})
    latest_verify = {r["ticket_key"]: r for r in db.all(
        "SELECT DISTINCT ON (ticket_key) ticket_key, outcome, details FROM runs"
        " WHERE ticket_key = ANY(%s) AND step='verify' AND status='finished'"
        " ORDER BY ticket_key, started_at DESC", (keys,))} if keys else {}
    added = {(r["ticket_key"], r["version_id"]): r["added_at"] for r in db.all(
        "SELECT ticket_key, version_id, added_at FROM ticket_version_added WHERE version_id = ANY(%s)", (ids,))}
    releases = {(r["project"], r["version_id"]): r for r in db.all(
        "SELECT * FROM version_releases WHERE version_id = ANY(%s)", (ids,))}
    members: dict[str, list[dict]] = {}
    for link in links:
        if link["ticket_key"] in by_key:
            members.setdefault(link["version_id"], []).append(by_key[link["ticket_key"]])
    out = []
    for v in versions:
        mine = members.get(v["id"], [])
        started = min((t["first_in_progress_at"] for t in mine if t.get("first_in_progress_at")), default=None)
        tickets = mark_late(mine, {t["key"]: added.get((t["key"], v["id"])) for t in mine}, started)
        readiness = release_readiness.evaluate(mine, by_key, latest_verify)
        release = _release(db, releases.get((v["project"], v["id"])), v)
        reasons = [] if v["released"] else readiness["reasons"] + _extra_reasons(v, release)
        out.append({"project": v["project"], "version_id": v["id"], "version": v["name"],
                    "released": bool(v["released"]), "release_date": str(v["release_date"] or "") or None,
                    "ready": bool(v["released"]) or (readiness["ready"] and not reasons),
                    "ticket_count": readiness["ticket_count"], "done_count": readiness["done_count"],
                    "started_at": _iso(started), "tickets": tickets,
                    "open_tickets": [t for t in tickets if t["status"] != "Done"],
                    "late_tickets": [t for t in tickets if t["added_late"]],
                    "reasons": reasons, "release": release})
    return out


def _select(db, projects: list[str]) -> list[dict]:
    rows = db.all("SELECT id, project, name, released, release_date FROM versions WHERE project = ANY(%s)"
                  " ORDER BY project, released, release_date DESC NULLS LAST, name, id", (projects,))
    shown: dict[tuple[str, bool], int] = {}
    out = []
    for r in rows:
        n = shown[(r["project"], r["released"])] = shown.get((r["project"], r["released"]), 0) + 1
        if not r["released"] or n <= RELEASED_SHOWN:
            out.append(r)
    return out


def project_versions(db, project: str) -> list[dict[str, Any]]:
    return _load(db, _select(db, [project]))


def all_versions(db, projects: Iterable[str]) -> list[dict[str, Any]]:
    return _load(db, _select(db, list(projects)))
