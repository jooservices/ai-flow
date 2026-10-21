"""Automatic, fenced releases for explicitly configured Jira project versions.

Version membership and status are read from Jira for each scan and again when a worker claims or completes a
release. Reviewer, QA, and QC evidence comes from the registry's independent verify runs, successful merge/QA
outcomes, and explicit green CI results respectively. Projects opt in by version name; TESTWP has a narrow
repo-and-version preset for the trial.
"""
from __future__ import annotations

import hashlib
import re
import string
import uuid
from typing import Any

from .db import J

TESTWP_DEFAULT = {
    "enabled": True,
    "versions": ["WordPress Core 0.5.0-beta", "WordPress Theme 0.5.0-beta"],
    "source_branch": "develop",
    "target_branch": "master",
}


def config(project: Any) -> dict[str, Any]:
    configured = getattr(project, "version_release", {}) or {}
    if configured:
        return configured
    if project.key == "TESTWP" and project.repo.lower().rstrip("/") == "jooservices/test-aiflow-wp":
        return TESTWP_DEFAULT
    return {}


def enabled(project: Any) -> bool:
    release = config(project)
    versions = release.get("versions") or []
    return (bool(release.get("enabled")) and bool(versions)
            and project.repo.lower().rstrip("/") != "jooservices/ai-flow")


def configured_names(project: Any) -> set[str]:
    versions = config(project).get("versions") or []
    if isinstance(versions, dict):
        return {str(name) for name in versions}
    return {str(name) for name in versions if str(name).strip()}


def source_branch(project: Any) -> str:
    release = config(project)
    return str(release.get("source_branch") or project.base or "develop")


def target_branch(project: Any) -> str:
    release = config(project)
    return str(release.get("target_branch") or "master")


def check_commands(project: Any) -> list[str]:
    release = config(project)
    commands = release.get("checks") or project.checks or []
    return [str(command).strip() for command in commands if str(command).strip()]


def release_tag(project: Any, version_name: str) -> str:
    release = config(project)
    prefixes = release.get("tag_prefixes") or {}
    safe_name = re.sub(r"[^a-z0-9.-]+", "-", version_name.lower()).strip("-.")
    prefix = str(prefixes.get(version_name) or "").strip()
    if prefix:
        safe_prefix = re.sub(r"[^a-z0-9.-]+", "-", prefix.lower()).strip("-.")
        version = re.search(r"\d+(?:\.\d+)+(?:-[0-9a-z.-]+)?", version_name.lower())
        suffix = version.group(0) if version else safe_name
        return f"{safe_prefix}-{suffix}"
    return f"{project.key.lower()}-{safe_name}"


def _tracker_row(ctx, project: str, version_id: str) -> dict | None:
    return ctx.db.one("SELECT * FROM version_releases WHERE project=%s AND version_id=%s", (project, version_id))


def ensure_tracker(ctx, project: Any, version: dict[str, Any]) -> str:
    """Create one Jira Release tracker per configured version, outside its fixVersion membership."""
    row = _tracker_row(ctx, project.key, version["id"])
    if row and row["tracker_key"]:
        return str(row["tracker_key"])
    issue_type = str(config(project).get("issue_type") or "Release")
    key = ctx.jira.create_issue(project.key, f"Release: {version['name']}",
                                f"AI-managed release tracker for Jira version {version['name']} "
                                f"(version id {version['id']}).", issuetype=issue_type,
                                labels=["aiflow-version-release"])
    ctx.db.run("INSERT INTO version_releases (project, version_id, version_name, tracker_key)"
               " VALUES (%s,%s,%s,%s) ON CONFLICT (project,version_id) DO UPDATE SET"
               " version_name=EXCLUDED.version_name, tracker_key=CASE WHEN version_releases.tracker_key='' "
               "THEN EXCLUDED.tracker_key ELSE version_releases.tracker_key END, updated_at=now()",
               (project.key, version["id"], version["name"], key))
    return key


def project_versions(ctx, project: Any) -> list[dict[str, Any]]:
    if not ctx.jira or not enabled(project):
        return []
    allowed = configured_names(project)
    return [version for version in ctx.jira.versions(project.key)
            if version["name"] in allowed and not version.get("released") and not version.get("archived")]


def _successful_evidence(ctx, key: str) -> tuple[bool, bool, bool]:
    dev = ctx.db.one("SELECT account_id, started_at FROM runs WHERE ticket_key=%s AND step='dev'"
                     " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (key,))
    if not dev:
        return False, False, False
    verify = ctx.db.one("SELECT account_id, outcome, details, started_at FROM runs WHERE ticket_key=%s"
                        " AND step='verify' AND status<>'running' AND started_at>%s"
                        " ORDER BY started_at DESC LIMIT 1", (key, dev["started_at"]))
    qa = ctx.db.one("SELECT outcome FROM runs WHERE ticket_key=%s AND step='qa' AND status<>'running'"
                    " AND started_at>%s ORDER BY started_at DESC LIMIT 1", (key, dev["started_at"]))
    details = (verify or {}).get("details") or {}
    reviewer = bool(verify and verify["account_id"] != dev["account_id"]
                    and verify["outcome"] in ("merged", "human")
                    and str(details.get("verdict", "")).upper() == "PASS")
    qa_passed = bool((verify and verify["outcome"] == "merged") or (qa and qa["outcome"] == "merged"))
    qc_passed = bool(verify and details.get("qc_passed") is True)
    return reviewer, qa_passed, qc_passed


def evaluate(ctx, project: Any, version: dict[str, Any]) -> dict[str, Any]:
    """Return a live Jira membership snapshot and every missing gate for each ticket."""
    issues = ctx.jira.version_issues(project.key, version["id"])
    tickets = []
    for issue in issues:
        fields = issue.get("fields") or {}
        key = str(issue.get("key") or "")
        status = str((fields.get("status") or {}).get("name") or "")
        reviewer, qa, qc = _successful_evidence(ctx, key)
        missing = []
        if status != "Done":
            missing.append("Done")
        if not reviewer:
            missing.append("reviewer PASS")
        if not qa:
            missing.append("QA merge")
        if not qc:
            missing.append("QC checks")
        tickets.append({"key": key, "summary": str(fields.get("summary") or key), "status": status,
                        "reviewer_passed": reviewer, "qa_passed": qa, "qc_passed": qc, "missing": missing})
    if not tickets:
        reasons = ["no tickets are currently assigned to this Jira version"]
    else:
        reasons = [f"{t['key']}: missing {', '.join(t['missing'])}" for t in tickets if t["missing"]]
    if not check_commands(project):
        reasons.append("no full project check commands are configured")
    return {"project": project.key, "version_id": str(version["id"]), "version": version["name"],
            "tickets": tickets, "ready": bool(tickets) and not reasons, "reasons": reasons}


def _snapshot_text(snapshot: dict[str, Any]) -> str:
    if snapshot["ready"]:
        return f"All {len(snapshot['tickets'])} current version tickets are Done and have reviewer, QA, and QC evidence."
    return "Release is blocked:\n" + "\n".join(f"- {reason}" for reason in snapshot["reasons"])


def record_gate(ctx, project: Any, version: dict[str, Any], snapshot: dict[str, Any], fence: int = 0) -> None:
    """Persist the live snapshot and leave an auditable Jira comment when the gate changes."""
    tracker = ensure_tracker(ctx, project, version)
    row = _tracker_row(ctx, project.key, version["id"])
    detail = _snapshot_text(snapshot)
    prior_detail = row["gate_detail"] if row else ""
    prior_status = row["status"] if row else "pending"
    status = prior_status if prior_status in ("checking", "released") else ("pending" if snapshot["ready"] else "blocked")
    ctx.db.run("UPDATE version_releases SET version_name=%s, status=%s, gate_detail=%s, ticket_snapshot=%s,"
               " updated_at=now() WHERE project=%s AND version_id=%s",
               (version["name"], status, detail, J(snapshot["tickets"]), project.key, version["id"]))
    if detail != prior_detail:
        ctx.jira.comment(tracker, f"AI Flow version release check · {version['name']}\n{detail}\n"
                         f"Current tickets: {', '.join(t['key'] for t in snapshot['tickets']) or '(none)'}")
        ctx.db.run("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload,fence)"
                   " VALUES ('jira','version-release-check',%s,'control',true,%s,%s,%s)",
                   (tracker, "ready" if snapshot["ready"] else "blocked", J(snapshot), fence or None))
        ctx.events.emit("version_release.gate", ticket=tracker, project=project.key, step="version_release",
                        outcome="ready" if snapshot["ready"] else "blocked", reason=detail,
                        data={"version_id": str(version["id"]), "version": version["name"],
                              "tickets": snapshot["tickets"]})


def scan(ctx) -> int:
    """Refresh every configured unreleased Jira version under a per-version Redis fence."""
    if not ctx.jira:
        return 0
    scanned = 0
    for project in ctx.repo().projects.values():
        if project.paused or not enabled(project):
            continue
        for version in project_versions(ctx, project):
            lock_key = version_identity(project.key, version["id"])
            run_id = str(uuid.uuid4())
            claim = ctx.leases.claim(lock_key, run_id, 300_000)
            if not claim.ok:
                continue
            try:
                snapshot = evaluate(ctx, project, version)
                if not ctx.leases.valid(lock_key, run_id, claim.fence):
                    continue
                record_gate(ctx, project, version, snapshot, claim.fence)
                scanned += 1
            finally:
                ctx.leases.release(lock_key, run_id, claim.fence)
    return scanned


def candidates(ctx, account: str, node: str) -> list[dict[str, Any]]:
    repo = ctx.repo()
    acc = repo.account(account)
    if not acc or acc.node != node or not ({"master", "release"} & set(acc.roles or [])):
        return []
    dbnode = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (node,)) or {}
    allowed_projects = set(repo.projects_for_node(node, dbnode.get("projects") or [], dbnode.get("dynamic", False)))
    out = []
    for project in repo.projects.values():
        if project.key not in allowed_projects or project.paused or not enabled(project):
            continue
        for version in project_versions(ctx, project):
            state = _tracker_row(ctx, project.key, version["id"])
            if state and state["status"] == "released":
                continue
            if state and state["status"] == "checking":
                running = ctx.db.one("SELECT 1 AS active FROM runs WHERE ticket_key=%s AND step='version_release'"
                                     " AND status='running' LIMIT 1",
                                     (version_identity(project.key, version["id"]),))
                if running:
                    continue
                _set_state(ctx, project.key, version["id"], status="pending")
            snapshot = evaluate(ctx, project, version)
            if snapshot["ready"]:
                out.append({**snapshot, "tracker_key": state["tracker_key"] if state else ""})
    return out


def tracker_for(ctx, project: Any, version_id: str, version_name: str) -> str:
    row = _tracker_row(ctx, project.key, version_id)
    if row and row["tracker_key"]:
        return row["tracker_key"]
    return ensure_tracker(ctx, project, {"id": version_id, "name": version_name})


def _changelog(snapshot: dict[str, Any], sha: str, checks: list[dict[str, Any]]) -> str:
    lines = [f"## {snapshot['version']}", "", "### Included tickets"]
    lines.extend(f"- [{ticket['key']}] {_markdown_text(ticket['summary'])}" for ticket in snapshot["tickets"])
    lines.extend(("", "### Release checks", f"- Passed on `{sha}`"))
    lines.extend(f"- `{item['command']}`: passed" for item in checks)
    return "\n".join(lines) + "\n"


def _markdown_text(value: str) -> str:
    """Keep Jira-controlled text on one Markdown line and render its punctuation literally."""
    normalized = re.sub(r"[\r\n]+", " ", str(value))
    return "".join(f"\\{char}" if char in string.punctuation else char for char in normalized)


def _audit(ctx, project: Any, tracker: str, run: dict, action: str, result: str, payload: dict) -> None:
    ctx.db.run("INSERT INTO write_audit (target,action,ticket,run_id,fence,actor,accepted,result,payload)"
               " VALUES (%s,%s,%s,%s,%s,%s,true,%s,%s)",
               ("github" if action.startswith("github-") else "jira", action, tracker, str(run["id"]),
                run["fence"], run["account_id"], result[:500], J(payload)))


def _set_state(ctx, project: str, version_id: str, **values: Any) -> None:
    columns = ", ".join(f"{name}=%s" for name in values)
    ctx.db.run(f"UPDATE version_releases SET {columns}, updated_at=now() WHERE project=%s AND version_id=%s",
               (*values.values(), project, version_id))


def _failed_checks(ctx, project: Any, version: dict[str, Any], tracker: str, run: dict,
                   failures: list[dict[str, Any]]) -> dict[str, Any]:
    bugs = []
    issue_type = str(config(project).get("bug_issue_type") or "Bug")
    for failure in failures:
        command = str(failure.get("command") or "release check")
        output = str(failure.get("output") or "")[-5000:]
        summary = f"Release check failed: {command}"[:250]
        description = (f"The automatic release check for {version['name']} failed on develop.\n\n"
                       f"Command: {command}\nExit code: {failure.get('exit_code', 'unknown')}\n\n"
                       f"Output:\n{output or '(no output)'}\n\nThis Bug blocks Jira release {version['name']}.")
        key = ctx.jira.create_issue(project.key, summary, description, issuetype=issue_type,
                                    labels=["aiflow-version-release", "release-check", "aiflow-ready"],
                                    fix_version=version["id"])
        ctx.jira.transition_to(key, "To Do")
        ctx.jira.link_issues(key, tracker, "Blocks")
        bugs.append({"key": key, "command": command})
        ctx.events.emit("version_release.bug_created", ticket=key, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"], node=run["node"],
                        outcome="To Do", reason=f"{command} failed; blocks release {tracker}",
                        data={"version_id": version["id"], "release": tracker})
    text = (f"Automatic release check failed on develop. Created {len(bugs)} Bug(s), each in Jira version "
            f"{version['name']} and linked to block this Release: "
            + ", ".join(bug["key"] for bug in bugs))
    ctx.jira.comment(tracker, text)
    ctx.db.run("UPDATE version_releases SET status='failed', checked_develop_sha=%s, gate_detail=%s,"
               " updated_at=now() WHERE project=%s AND version_id=%s",
               (run.get("head_sha") or "", text, project.key, version["id"]))
    _audit(ctx, project, tracker, run, "jira-release-check-failed", text, {"bugs": bugs, "failures": failures})
    ctx.events.emit("version_release.check", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], account=run["account_id"], node=run["node"],
                    outcome="failed", reason=text, data={"version_id": version["id"], "bugs": bugs})
    return {"released": False, "status": "failed", "bugs": bugs}


def complete(ctx, project: Any, version: dict[str, Any], run: dict, report: dict[str, Any]) -> dict[str, Any]:
    """Accept a fenced worker's check report and perform an idempotent release if it still passes live gates."""
    tracker = tracker_for(ctx, project, version["id"], version["name"])
    snapshot = evaluate(ctx, project, version)
    if not snapshot["ready"]:
        detail = _snapshot_text(snapshot)
        _set_state(ctx, project.key, version["id"], status="blocked", gate_detail=detail,
                   ticket_snapshot=J(snapshot["tickets"]))
        ctx.jira.comment(tracker, f"Release stopped before publication because the live version gate changed.\n{detail}")
        ctx.events.emit("version_release.check", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], outcome="blocked", reason=detail,
                        data={"version_id": version["id"], "tickets": snapshot["tickets"]})
        return {"released": False, "status": "blocked", "reasons": snapshot["reasons"]}
    failures = [f for f in report.get("checks", []) if not f.get("passed")]
    if report.get("error"):
        failures.append({"command": "prepare release checkout", "exit_code": -1, "output": report["error"]})
    if failures:
        return _failed_checks(ctx, project, version, tracker, run, failures)

    sha = str(report.get("head_sha") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return _failed_checks(ctx, project, version, tracker, run,
                              [{"command": "resolve checked develop commit", "exit_code": -1,
                                "output": "The release worker did not report a valid checked commit SHA."}])
    repo = project.repo_name
    source, target = source_branch(project), target_branch(project)
    if ctx.github is None:
        raise RuntimeError("GitHub App is not connected")
    current_source =ctx.github.branch_sha(repo, source)
    if current_source.lower() != sha.lower():
        detail = f"develop moved during the full check ({sha[:12]} checked, {current_source[:12]} now); retrying."
        _set_state(ctx, project.key, version["id"], status="pending", checked_develop_sha=sha, gate_detail=detail)
        ctx.jira.comment(tracker, f"Full checks passed but publication stopped: {detail}")
        ctx.events.emit("version_release.check", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], outcome="stale", reason=detail,
                        data={"version_id": version["id"], "checked_sha": sha, "current_sha": current_source})
        return {"released": False, "status": "stale", "reason": detail}

    message = f"release: {version['name']} ({project.key})"
    merge = ctx.github.merge_branch(repo, target, source, message)
    master_sha = str(merge.get("sha") or ctx.github.branch_sha(repo, target))
    _audit(ctx, project, tracker, run, "github-merge", master_sha,
           {"source": source, "target": target, "checked_sha": sha})
    ctx.events.emit("version_release.merge", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="merged", reason=master_sha,
                    data={"source": source, "target": target, "checked_sha": sha})
    tag = release_tag(project, version["name"])
    existing_tag_sha = ctx.github.tag_sha(repo, tag)
    if existing_tag_sha and existing_tag_sha.lower() != master_sha.lower():
        raise RuntimeError(f"release tag {tag} already points to {existing_tag_sha}, expected {master_sha}")
    tag_created = not existing_tag_sha
    if tag_created:
        ctx.github.create_tag(repo, tag, master_sha)
    _audit(ctx, project, tracker, run, "github-tag", "created" if tag_created else "existing",
           {"tag": tag, "sha": master_sha, "repo": project.repo})
    ctx.events.emit("version_release.tag", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="created" if tag_created else "existing",
                    reason=tag, data={"sha": master_sha, "version_id": version["id"]})
    release = ctx.github.release_by_tag(repo, tag)
    notes = _changelog(snapshot, master_sha, report.get("checks", []))
    release_created = not release
    if release_created:
        release = ctx.github.create_release(repo, tag, version["name"], notes)
        _audit(ctx, project, tracker, run, "github-release", str(release.get("html_url") or tag),
               {"tag": tag, "sha": master_sha})
    release_url = str(release.get("html_url") or "")
    ctx.events.emit("version_release.github_release", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="created" if release_created else "existing",
                    reason=release_url or tag, data={"tag": tag, "sha": master_sha, "url": release_url})
    # Jira version Released is last among publication steps; retries reconcile the same merge/tag/release.
    latest_version = next((v for v in ctx.jira.versions(project.key) if v["id"] == version["id"]), version)
    if not latest_version.get("released"):
        ctx.jira.release_version(version["id"])
    _set_state(ctx, project.key, version["id"], status="released", checked_develop_sha=sha,
               master_sha=master_sha, tag=tag, github_release_url=release_url,
               gate_detail=f"Released at {tag} ({master_sha}).")
    _audit(ctx, project, tracker, run, "jira-version-released", "Released",
           {"version_id": version["id"], "commit": master_sha, "tag": tag, "release_url": release_url,
            "tickets": snapshot["tickets"]})
    ctx.events.emit("version_release.jira_version", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="Released", reason=version["name"],
                    data={"version_id": version["id"], "tag": tag, "master_sha": master_sha,
                          "release_url": release_url})
    ctx.jira.comment(tracker, f"Version released.\nChecked tickets: {', '.join(t['key'] for t in snapshot['tickets'])}\n"
                     f"Checks passed on develop commit: {sha}\nCommit on {target}: {master_sha}\nTag: {tag}\n"
                     f"GitHub Release: {release_url or tag}\nJira version: Released")
    ctx.events.emit("version_release.completed", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], account=run["account_id"], node=run["node"],
                    outcome="released", reason=f"{tag} {release_url}",
                    data={"tickets": snapshot["tickets"], "checked_sha": sha, "master_sha": master_sha,
                          "tag": tag, "release_url": release_url, "jira_version": "Released"})
    try:
        back = ctx.github.merge_branch(repo, source, target, f"chore: merge {target} back after {tag}")
        back_sha = str(back.get("sha") or ctx.github.branch_sha(repo, source))
        ctx.jira.comment(tracker, f"Merge-back complete: {target} into {source} at {back_sha}.")
        ctx.events.emit("version_release.merge_back", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], outcome="merged", reason=back_sha,
                        data={"source": target, "target": source})
    except Exception as exc:  # noqa: BLE001 - published release remains valid; leave an actionable Jira task
        task = ctx.jira.create_issue(project.key, f"Merge {target} back after {version['name']}",
                                     f"Release {tag} is published. Merge {target} into {source} and resolve any conflicts.\n"
                                     f"Error: {str(exc)[:2000]}", issuetype="Task",
                                     labels=["aiflow-version-release", "merge-back", "aiflow-ready"])
        ctx.jira.transition_to(task, "To Do")
        ctx.jira.comment(tracker, f"Merge-back failed; follow-up ticket {task} created. {str(exc)[:500]}")
        ctx.events.emit("version_release.merge_back", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], outcome="ticket_created", reason=task,
                        data={"error": str(exc)[:2000]})
    return {"released": True, "status": "released", "tag": tag, "sha": master_sha,
            "release_url": release_url}


def version_identity(project: str, version_id: str) -> str:
    """Stable synthetic run key; never a Jira ticket and safe as a Redis lease identifier."""
    digest = hashlib.sha256(f"{project}:{version_id}".encode()).hexdigest()[:16]
    return f"release:{project}:{digest}"
