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

from . import release_readiness, scale_proof

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
    return _release_steps_many(db, [(tracker, status)])[(tracker, status)]


def _release_step_rows(db, trackers: list[str]) -> tuple[dict, dict, dict]:
    actions = ("github-release-files", "github-merge", "github-tag", "github-release")
    audits_by_tracker: dict[str, dict] = {}
    for row in db.all("SELECT ticket, id, ts, action, run_id, result, payload FROM write_audit"
                      " WHERE ticket=ANY(%s) AND action=ANY(%s) ORDER BY id", (trackers, list(actions))):
        audits_by_tracker.setdefault(row["ticket"], {})[row["action"]] = row
    checks_by_tracker = {}
    for row in db.all("SELECT ticket, ts, run_id, outcome, reason FROM events"
                      " WHERE ticket=ANY(%s) AND type='version_release.check' ORDER BY id DESC", (trackers,)):
        checks_by_tracker.setdefault(row["ticket"], row)
    run_ids = {row["run_id"] for audits in audits_by_tracker.values() for row in audits.values() if row["run_id"]}
    run_ids.update(row["run_id"] for row in checks_by_tracker.values() if row["run_id"])
    nodes = {str(row["id"]): row["node"] for row in db.all(
        "SELECT id, node FROM runs WHERE id::text=ANY(%s)", (list(run_ids),))} if run_ids else {}
    return audits_by_tracker, checks_by_tracker, nodes


def _release_step(name: str, state: str, row: dict | None, nodes: dict, detail: str = "") -> dict[str, Any]:
    run_id = (row or {}).get("run_id") or ""
    return _step(name, state, run_id=run_id, node=nodes.get(run_id, ""), detail=detail,
                 at=_iso((row or {}).get("ts")))


def _steps_for_tracker(status: str, audits: dict, check: dict | None, nodes: dict) -> list[dict[str, Any]]:
    files, merge = audits.get("github-release-files"), audits.get("github-merge")
    tag, release = audits.get("github-tag"), audits.get("github-release")
    payload = (merge or {}).get("payload") or {}
    steps = []
    if files or merge:
        steps.append(_release_step("check", "done", files or merge, nodes, "release checks passed"))
    elif check and check["outcome"] in ("failed", "blocked", "stale"):
        steps.append(_release_step("check", "failed", check, nodes, check["reason"][:300]))
    else:
        steps.append(_step("check", "running" if status == "checking" else "pending"))
    steps.append(_release_step("changelog", "done", files, nodes,
                               ", ".join((files or {}).get("payload", {}).get("files") or []))
                 if files else _step("changelog"))
    pr_label = f"#{payload['pr_number']}" if payload.get("pr_number") else ""
    steps.append({**_release_step("pr", "done", merge, nodes, pr_label), "url": payload.get("pr_url", "")}
                 if merge else _step("pr"))
    steps.append(_release_step("merge", "done", merge, nodes, (merge["result"] or "")[:12])
                 if merge else _step("merge"))
    steps.append(_release_step("tag", "done", tag, nodes, (tag["payload"] or {}).get("tag", ""))
                 if tag else _step("tag"))
    steps.append({**_release_step("release", "done", release, nodes, ""), "url": release["result"]
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


def _release_steps_many(db, tracked_releases: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    trackers = list(dict.fromkeys(tracker for tracker, _ in tracked_releases))
    if not trackers:
        return {}
    audits, checks, nodes = _release_step_rows(db, trackers)
    return {(tracker, status): _steps_for_tracker(status, audits.get(tracker, {}), checks.get(tracker), nodes)
            for tracker, status in dict.fromkeys(tracked_releases)}


def _release(row: dict | None, version: dict, steps_by_release: dict) -> dict[str, Any]:
    if not row:
        return {"status": "released" if version["released"] else "none", "tracker": "", "tag": "",
                "github_release_url": "", "detail": "", "steps": []}
    status = "released" if version["released"] and row["status"] == "released" else row["status"]
    return {"status": status, "tracker": row["tracker_key"], "tag": row["tag"],
            "github_release_url": row["github_release_url"], "detail": row["gate_detail"],
            "steps": steps_by_release.get((row["tracker_key"], row["status"]), []) if row["tracker_key"] else []}


def _extra_reasons(version: dict, release: dict, proof: dict | None = None) -> list[dict[str, Any]]:
    if version["released"]:
        return []
    out = []
    if release["status"] == "checking":
        out.append({"cause": "release_running", "ticket": None, "summary": "", "detail": release["detail"]})
    if release["status"] == "failed":
        out.append({"cause": "release_failed", "ticket": None, "summary": "", "detail": release["detail"]})
    if proof is None:
        out.append({"cause": "scale_proof_missing", "ticket": None, "summary": "",
                    "detail": "No scale proof run is attached to this release"})
    elif not proof["passed"]:
        out.append({"cause": "scale_proof_failed", "ticket": None, "summary": "", "detail": proof["blocker"]})
    return out


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
    steps_by_release = _release_steps_many(
        db, [(row["tracker_key"], row["status"]) for row in releases.values() if row["tracker_key"]])
    proofs = scale_proof.attached(db, [v["name"] for v in versions])  # scale-proof run attached to the version
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
        release = _release(releases.get((v["project"], v["id"])), v, steps_by_release)
        proof = proofs.get(v["name"])
        reasons = [] if v["released"] else readiness["reasons"] + _extra_reasons(v, release, proof)
        out.append({"project": v["project"], "version_id": v["id"], "version": v["name"],
                    "released": bool(v["released"]), "release_date": str(v["release_date"] or "") or None,
                    "ready": bool(v["released"]) or (readiness["ready"] and not reasons),
                    "ticket_count": readiness["ticket_count"], "done_count": readiness["done_count"],
                    "started_at": _iso(started), "tickets": tickets,
                    "open_tickets": [t for t in tickets if t["status"] != "Done"],
                    "late_tickets": [t for t in tickets if t["added_late"]],
                    "reasons": reasons, "release": release, "scale_proof": proof})
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
