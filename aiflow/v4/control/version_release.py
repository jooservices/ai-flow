"""Automatic, fenced releases for explicitly configured Jira project versions.

Version membership and status are read from Jira for each scan and again when a worker claims or completes a
release. Reviewer, QA, and QC evidence comes from the registry's independent verify runs, successful merge/QA
outcomes, and explicit green CI results respectively. Projects opt in by version name; TESTWP has a narrow
repo-and-version preset for the trial.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import string
import time
import uuid
from typing import Any

from .db import J

log = logging.getLogger(__name__)

FAILURE_ACTIONS = ("jira-release-check-failed", "jira-release-publish-failed")
MERGE_BACK_ACTIONS = ("github-merge-back", "jira-merge-back-task")
JIRA_RETRY_S = 3600  # a publish error whose Jira follow-up could not be created waits this long before a retry

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
    commands = (release.get("release_check") or project.release_check or release.get("checks")
                or project.checks or [])
    return [str(command).strip() for command in commands if str(command).strip()]


def release_tag(project: Any, version_name: str) -> str:
    release = config(project)
    prefixes = release.get("tag_prefixes") or {}
    safe_name = re.sub(r"[^a-z0-9.-]+", "-", version_name.lower()).strip("-.")
    prefix = str(prefixes.get(version_name) or release.get("tag_prefix") or project.tag_prefix or "").strip()
    if prefix:
        safe_prefix = re.sub(r"[^a-z0-9.-]+", "-", prefix.lower()).strip("-.")
        version = re.search(r"\d+(?:\.\d+)+(?:-[0-9a-z.-]+)?", version_name.lower())
        suffix = version.group(0) if version else safe_name
        return f"{safe_prefix}-{suffix}"
    return f"{project.key.lower()}-{safe_name}"


def release_branch(project: Any, version_name: str, source_sha: str) -> str:
    """Use one branch per checked source commit so retries never mix releases from different develop heads."""
    version = re.sub(r"[^a-z0-9.-]+", "-", version_name.lower()).strip("-.")[:72] or "version"
    return f"release/{version}-{source_sha[:10].lower()}"


def release_version_number(version_name: str) -> str:
    match = re.search(r"\d+(?:\.\d+)+(?:-[0-9a-z.-]+)?", version_name.lower())
    if not match:
        raise ValueError(f"Jira version {version_name!r} does not contain a numeric release version")
    return match.group(0)


def _readme_with_version(content: str, snapshot: dict[str, Any]) -> str:
    version_name = str(snapshot["version"])
    number = release_version_number(version_name)
    start, end = "<!-- aiflow-release-version -->", "<!-- /aiflow-release-version -->"
    lines = [start, "## Current release", f"Current version: `{number}` ({_markdown_text(version_name)}).",
             f"Released: {time.strftime('%Y-%m-%d', time.gmtime())}", "", "Included tickets:"]
    lines.extend(f"- [{ticket['key']}] {_markdown_text(ticket['summary'])}" for ticket in snapshot["tickets"])
    lines.append(end)
    block = "\n".join(lines)
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(content):
        return pattern.sub(block, content, count=1)
    heading = re.search(r"(?m)^# .+$", content)
    if heading:
        at = heading.end()
        return content[:at] + "\n\n" + block + content[at:].lstrip("\n")
    return block + ("\n\n" + content if content else "\n")


def _update_manifest(path: str, content: str, version_name: str) -> str:
    """Update an existing top-level version value in a supported project manifest, preserving other data."""
    number = release_version_number(version_name)
    basename = path.rsplit("/", 1)[-1].lower()
    if basename in {"package.json", "composer.json", "package-lock.json"}:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return content
        changed = False
        if isinstance(data, dict) and "version" in data:
            data["version"] = number
            changed = True
        if (basename == "package-lock.json" and isinstance(data, dict)
                and isinstance(data.get("packages"), dict) and isinstance(data["packages"].get(""), dict)
                and "version" in data["packages"][""]):
            data["packages"][""]["version"] = number
            changed = True
        if changed:
            indent = 4 if re.search(r"\n {4}\"", content) else 2
            return json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
        return content

    if basename in {"pyproject.toml", "cargo.toml", "setup.cfg", "chart.yaml"}:
        lines = content.splitlines(keepends=True)
        section = ""
        changed = False
        for index, line in enumerate(lines):
            match = re.match(r"\s*\[([^]]+)\]", line)
            if match:
                section = match.group(1).strip().lower()
            should_update = (basename == "pyproject.toml" and section in {"project", "tool.poetry"}
                             and re.match(r"\s*version\s*=", line)
                             or basename == "cargo.toml" and not section and re.match(r"\s*version\s*=", line)
                             or basename == "setup.cfg" and section == "metadata"
                             and re.match(r"\s*version\s*=", line)
                             or basename == "chart.yaml" and re.match(r"\s*(version|appVersion)\s*:", line))
            if should_update:
                key = "version|appVersion" if basename == "chart.yaml" else "version"
                quoted = re.sub(r"(?m)^(\s*(?:" + key + r")\s*(?:=|:)\s*)([^\s#]+)(.*)$",
                                lambda m: f"{m.group(1)}{json.dumps(number)}{m.group(3)}", line)
                if quoted != line:
                    lines[index] = quoted
                    changed = True
                    if basename != "chart.yaml":
                        break
        return "".join(lines) if changed else content

    if basename.endswith(".xml"):
        return re.sub(r"(?is)(<Version>)[^<]+(</Version>)",
                      lambda match: f"{match.group(1)}{number}{match.group(2)}", content, count=1)
    return content


def _manifest_paths(ctx, project: Any, repo: str, branch: str) -> list[str]:
    configured = config(project).get("manifest_files")
    if configured is not None:
        return sorted({str(path).strip("/") for path in configured if str(path).strip("/")})
    names = {"package.json", "composer.json", "package-lock.json", "pyproject.toml", "cargo.toml",
             "setup.cfg", "chart.yaml"}
    return [path for path in ctx.github.repository_files(repo, branch)
            if path.rsplit("/", 1)[-1].lower() in names or path.lower().endswith(".csproj")]


def _prepare_release_files(ctx, project: Any, repo: str, branch: str, snapshot: dict[str, Any],
                           sha: str, checks: list[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    """Prepare idempotent release files on the release branch; callers commit only changed content."""
    notes = _changelog(snapshot, sha, checks)
    changelog_path = str(config(project).get("changelog_file") or "CHANGELOG.md")
    readme_path = str(config(project).get("readme_file") or "README.md")
    current = ctx.github.file_content(repo, branch, changelog_path) or ""
    heading = f"## {_markdown_text(snapshot['version'])}"
    blocks = re.split(r"(?=^## )", current, flags=re.MULTILINE)
    if any(block.splitlines() and (block.splitlines()[0] == heading
                                   or block.splitlines()[0].startswith(heading + " - ")) for block in blocks):
        changelog = current
    elif current:
        changelog = current.rstrip() + "\n\n" + notes
    else:
        changelog = notes
    readme = ctx.github.file_content(repo, branch, readme_path) or ""
    files = {changelog_path: changelog, readme_path: _readme_with_version(readme, snapshot)}
    for path in _manifest_paths(ctx, project, repo, branch):
        content = ctx.github.file_content(repo, branch, path)
        if content is None:
            continue
        updated = _update_manifest(path, content, snapshot["version"])
        if updated != content:
            files[path] = updated
    return notes, files


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
                                labels=["aiflow-version-release"], version_field=version["name"])
    ctx.db.run("INSERT INTO version_releases (project, version_id, version_name, tracker_key)"
               " VALUES (%s,%s,%s,%s) ON CONFLICT (project,version_id) DO UPDATE SET"
               " version_name=EXCLUDED.version_name, tracker_key=CASE WHEN version_releases.tracker_key='' "
               "THEN EXCLUDED.tracker_key ELSE version_releases.tracker_key END, updated_at=now()",
               (project.key, version["id"], version["name"], key))
    return key


def configured_versions(ctx, project: Any) -> list[dict[str, Any]]:
    if not ctx.jira or not enabled(project):
        return []
    allowed = configured_names(project)
    return [version for version in ctx.jira.versions(project.key)
            if version["name"] in allowed and not version.get("archived")]


def project_versions(ctx, project: Any) -> list[dict[str, Any]]:
    """Return configured versions that Jira still considers unreleased."""
    return [version for version in configured_versions(ctx, project) if not version.get("released")]


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
        tickets.append({"key": key, "summary": str(fields.get("summary") or key),
                        "type": str((fields.get("issuetype") or {}).get("name") or "Story"), "status": status,
                        "reviewer_passed": reviewer, "qa_passed": qa, "qc_passed": qc, "missing": missing})
    if not tickets:
        reasons = ["no tickets are currently assigned to this Jira version"]
    else:
        reasons = [f"{t['key']}: missing {', '.join(t['missing'])}" for t in tickets if t["missing"]]
    if not check_commands(project):
        reasons.append("no full project check commands are configured")
    reasons.extend(_failure_reasons(ctx, project, version, tickets))
    return {"project": project.key, "version_id": str(version["id"]), "version": version["name"],
            "tickets": tickets, "ready": bool(tickets) and not reasons, "reasons": reasons}


def _last_failure(ctx, tracker: str) -> dict | None:
    return ctx.db.one("SELECT action, payload FROM write_audit WHERE ticket=%s AND action IN (%s, %s)"
                      " ORDER BY id DESC LIMIT 1", (tracker, *FAILURE_ACTIONS))


def _failure_reasons(ctx, project: Any, version: dict[str, Any], tickets: list[dict[str, Any]]) -> list[str]:
    """Keep a `failed` version out of release until the blockers recorded by its last failure are resolved.

    Release-check Bugs are deliberately kept out of the Jira version membership: they remain in Backlog and
    only their own status must reach Done before the full release checks are retried. Publication follow-up
    Tasks also stay outside the version and only have to reach Done.
    """
    row = _tracker_row(ctx, project.key, version["id"])
    if not row or row["status"] != "failed" or not row["tracker_key"]:
        return []
    last = _last_failure(ctx, row["tracker_key"])
    if not last:
        return []
    payload = last["payload"] or {}
    reasons = []
    for bug in payload.get("bugs") or []:
        key = str(bug.get("key") or "")
        if key:
            try:
                status = ctx.jira.status(key)
            except Exception:  # noqa: BLE001 - an unreadable release Bug keeps publication blocked
                status = "unknown"
            if status != "Done":
                reasons.append(f"{key}: release-check Bug is {status}, not Done")
    for blocker in payload.get("blockers") or []:
        key = str(blocker.get("key") or "")
        try:
            status = ctx.jira.status(key)
        except Exception:  # noqa: BLE001 - an unreadable blocker keeps the version waiting
            status = "unknown"
        if status != "Done":
            reasons.append(f"{key}: release follow-up is {status}, not Done")
    retry_after = float(payload.get("retry_after") or 0)
    if retry_after > time.time():
        reasons.append("publication failed and its Jira follow-up could not be created; retrying after "
                       + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(retry_after)))
    return reasons


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
    status = prior_status if prior_status in ("checking", "released", "failed") else ("pending" if snapshot["ready"] else "blocked")
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
    """Refresh configured versions and reconcile releases under a per-version Redis fence."""
    if not ctx.jira:
        return 0
    scanned = 0
    for project in ctx.repo().projects.values():
        if project.paused or not enabled(project):
            continue
        for version in configured_versions(ctx, project):
            lock_key = version_identity(project.key, version["id"])
            run_id = str(uuid.uuid4())
            claim = ctx.leases.claim(lock_key, run_id, 300_000)
            if not claim.ok:
                continue
            try:
                if version.get("released"):
                    if ctx.leases.valid(lock_key, run_id, claim.fence):
                        run = {"id": run_id, "fence": claim.fence, "account_id": "control", "node": ""}
                        if reconcile_released_version(ctx, project, version, run):
                            scanned += 1
                    continue
                snapshot = evaluate(ctx, project, version)
                if not ctx.leases.valid(lock_key, run_id, claim.fence):
                    continue
                record_gate(ctx, project, version, snapshot, claim.fence)
                scanned += 1
            finally:
                ctx.leases.release(lock_key, run_id, claim.fence)
        reconcile_merge_backs(ctx, project)
    return scanned


def _release_audit(ctx, tracker: str, action: str) -> dict | None:
    return ctx.db.one("SELECT result,payload FROM write_audit WHERE ticket=%s AND action=%s"
                      " ORDER BY id DESC LIMIT 1", (tracker, action))


def reconcile_released_version(ctx, project: Any, version: dict[str, Any], run: dict) -> bool:
    """Repair registry state after Jira reports a release whose final registry write was interrupted.

    A version is considered one of ours only when the tracker has the preceding GitHub merge, tag, and Release
    audit records. Those writes happen before the Jira Released call, so they provide enough durable data to
    finish the registry record without repeating publication.
    """
    row = _tracker_row(ctx, project.key, version["id"])
    if not version.get("released") or not row or not row["tracker_key"]:
        return False
    tracker = str(row["tracker_key"])
    merge = _release_audit(ctx, tracker, "github-merge")
    tag_audit = _release_audit(ctx, tracker, "github-tag")
    release_audit = _release_audit(ctx, tracker, "github-release")
    if not merge or not tag_audit or not release_audit:
        return False

    merge_payload = merge["payload"] or {}
    tag_payload = tag_audit["payload"] or {}
    release_payload = release_audit["payload"] or {}
    master_sha = str(merge.get("result") or "")
    tag = str(tag_payload.get("tag") or "")
    if (not master_sha or not tag or str(release_payload.get("tag") or "") != tag
            or str(tag_payload.get("sha") or "").lower() != master_sha.lower()
            or str(release_payload.get("sha") or "").lower() != master_sha.lower()):
        return False

    release_url = str(release_audit.get("result") or "")
    if release_url == tag:
        release_url = ""
    checked_sha = str(merge_payload.get("checked_sha") or row.get("checked_develop_sha") or "")
    detail = f"Released at {tag} ({master_sha}); registry state reconciled from publication audit."
    if (row["status"] != "released" or row.get("master_sha") != master_sha or row.get("tag") != tag
            or row.get("github_release_url") != release_url):
        _set_state(ctx, project.key, version["id"], status="released", checked_develop_sha=checked_sha,
                   master_sha=master_sha, tag=tag, github_release_url=release_url, gate_detail=detail)

    if not _release_audit(ctx, tracker, "jira-version-released"):
        payload = {"version_id": version["id"], "commit": master_sha, "tag": tag,
                   "release_url": release_url, "tickets": row.get("ticket_snapshot") or []}
        _audit(ctx, project, tracker, run, "jira-version-released", "Released", payload)
        _best_effort("release reconciliation event", ctx.events.emit, "version_release.jira_version",
                     ticket=tracker, project=project.key, step="version_release", run_id=str(run["id"]),
                     fence=run["fence"], outcome="Released", reason=version["name"], data=payload)
        _best_effort("release reconciliation comment", ctx.jira.comment, tracker,
                     f"Recovered release registry state from the completed publication.\n"
                     f"Commit on {target_branch(project)}: {master_sha}\nTag: {tag}\n"
                     f"GitHub Release: {release_url or tag}\nJira version: Released")
    return True


def reconcile_merge_backs(ctx, project: Any) -> int:
    """Retry merge-back for released versions whose release run stopped before recording one.

    The Jira version is already Released, so it no longer appears in `project_versions`; this durable path reads
    released rows from the registry and runs the same idempotent `merge_back` under the version lease.
    """
    if ctx.github is None:
        return 0
    done = 0
    rows = ctx.db.all("SELECT * FROM version_releases WHERE project=%s AND status='released'", (project.key,))
    for row in rows:
        if not row["tracker_key"] or _merge_back_recorded(ctx, row["tracker_key"]):
            continue
        lock_key = version_identity(project.key, row["version_id"])
        run_id = str(uuid.uuid4())
        claim = ctx.leases.claim(lock_key, run_id, 300_000)
        if not claim.ok:
            continue
        try:
            if ctx.leases.valid(lock_key, run_id, claim.fence):
                merge_back(ctx, project, row["tracker_key"], row["version_name"], row["tag"],
                           {"id": run_id, "fence": claim.fence, "account_id": "control", "node": ""})
                done += 1
        except Exception:  # noqa: BLE001 - nothing was recorded, so the next scan retries
            log.exception("merge-back retry failed for %s %s", project.key, row["version_name"])
        finally:
            ctx.leases.release(lock_key, run_id, claim.fence)
    return done


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
    date = time.strftime("%Y-%m-%d", time.gmtime())
    lines = [f"## {_markdown_text(snapshot['version'])} - {date}", ""]
    groups = {"Added": [], "Changed": [], "Fixed": []}
    for ticket in snapshot["tickets"]:
        kind = str(ticket.get("type") or "Story").lower()
        section = "Fixed" if kind == "bug" else "Added" if kind in {"story", "feature", "new feature", "epic"} else "Changed"
        groups[section].append(f"- [{ticket['key']}] {_markdown_text(ticket['summary'])}")
    for section in ("Added", "Changed", "Fixed"):
        if groups[section]:
            lines.extend((f"### {section}", *groups[section], ""))
    lines.extend(("### Release checks", f"- Passed on `{sha}`"))
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
    prior = ctx.db.one("SELECT payload FROM write_audit WHERE ticket=%s AND action='jira-release-check-failed'"
                       " AND run_id=%s ORDER BY id DESC LIMIT 1", (tracker, str(run["id"])))
    if prior:
        payload = prior["payload"] or {}
        return {"released": False, "status": "failed", "bugs": payload.get("bugs") or []}
    bugs = []
    issue_type = str(config(project).get("bug_issue_type") or "Bug")
    for failure in failures:
        command = str(failure.get("command") or "release check")
        output = str(failure.get("output") or "")[-5000:]
        summary = f"Release check failed: {command}"[:250]
        description = (f"The automatic release check for {version['name']} failed on its release branch.\n\n"
                       f"Command: {command}\nExit code: {failure.get('exit_code', 'unknown')}\n\n"
                       f"Output:\n{output or '(no output)'}\n\nThis Bug blocks Jira release {version['name']}.")
        key = ctx.jira.create_issue(project.key, summary, description, issuetype=issue_type,
                                    labels=["aiflow-version-release", "release-check", "release-qa", "aiflow-ready"])
        ctx.jira.transition_to(key, "Backlog")
        ctx.jira.link_issues(key, tracker, "Blocks")
        bugs.append({"key": key, "command": command})
        ctx.events.emit("version_release.bug_created", ticket=key, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"], node=run["node"],
                        outcome="Backlog", reason=f"{command} failed; blocks release {tracker}",
                        data={"version_id": version["id"], "release": tracker})
    text = (f"Automatic release check failed on release branch. Created {len(bugs)} Bug(s) in Backlog, each "
            f"labelled release-qa and linked to block this Release: "
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


def publish_failed(ctx, project: Any, version: dict[str, Any], run: dict, exc: Exception) -> dict[str, Any]:
    """Park a version after a GitHub/Jira publication error behind one blocking follow-up Task.

    The version stays `failed` (never `pending`), so workers do not reclaim it and rerun the full checks on
    every poll; it becomes a candidate again once the Task is Done. The same error with its Task still open
    reuses that Task and posts no new comment.
    """
    tracker = tracker_for(ctx, project, version["id"], version["name"])
    error = str(exc)[:1000]
    message = f"Automatic publication stopped at an integration error: {error}"
    row = _tracker_row(ctx, project.key, version["id"])
    if row and row["status"] == "released":
        return {"released": True, "status": "released", "reason": message}
    try:
        latest_version = next((item for item in ctx.jira.versions(project.key)
                               if str(item["id"]) == str(version["id"])), version)
    except Exception:  # noqa: BLE001 - retain the normal failure path if Jira cannot confirm Released
        latest_version = version
    if latest_version.get("released"):
        try:
            if reconcile_released_version(ctx, project, latest_version, run):
                return {"released": True, "status": "released", "reason": "Jira release reconciled"}
        except Exception:  # noqa: BLE001 - the scheduled scan will retry this durable reconciliation
            log.exception("release registry reconciliation failed for %s %s", project.key, version["name"])
        return {"released": False, "status": "reconciling",
                "reason": "Jira reports Released; registry reconciliation will retry on the next scan."}
    _set_state(ctx, project.key, version["id"], status="failed", gate_detail=message)
    last = _last_failure(ctx, tracker)
    prior = (last["payload"] or {}) if last and last["action"] == "jira-release-publish-failed" else {}
    same_error = prior.get("error") == error
    task = ""
    if same_error:
        for blocker in prior.get("blockers") or []:
            try:
                if ctx.jira.status(blocker["key"]) != "Done":
                    task = str(blocker["key"])
            except Exception:  # noqa: BLE001 - unknown status: create a fresh follow-up below
                pass
    payload: dict[str, Any] = {"error": error, "version_id": version["id"]}
    try:
        created = not task
        if created:
            task = ctx.jira.create_issue(
                project.key, f"Release publication failed: {version['name']}"[:250],
                f"The full checks for Jira version {version['name']} passed, but publication stopped:\n\n{error}\n\n"
                f"Fix the cause (for example a merge conflict between {source_branch(project)} and "
                f"{target_branch(project)}, branch protection, or GitHub access) and move this Task to Done. "
                f"AI Flow then checks and releases the version again.",
                issuetype="Task", labels=["aiflow-version-release", "release-publish"])
            ctx.jira.link_issues(task, tracker, "Blocks")
        payload["blockers"] = [{"key": task}]
        if created or not same_error:
            ctx.jira.comment(tracker, f"{message}\nRelease is blocked by {task} until it is Done.")
    except Exception as jira_exc:  # noqa: BLE001 - without a Jira blocker, fall back to a timed retry
        payload["jira_error"] = str(jira_exc)[:500]
        payload["retry_after"] = time.time() + JIRA_RETRY_S
    _audit(ctx, project, tracker, run, "jira-release-publish-failed", message, payload)
    ctx.events.emit("version_release.error", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="failed", reason=message,
                    data={"version_id": version["id"], "blockers": payload.get("blockers", []),
                          "retry_after": payload.get("retry_after")})
    return {"released": False, "status": "failed", "reason": message, "blockers": payload.get("blockers", [])}


def complete(ctx, project: Any, version: dict[str, Any], run: dict, report: dict[str, Any]) -> dict[str, Any]:
    """Accept a fenced worker's check report and perform an idempotent release if it still passes live gates."""
    tracker = tracker_for(ctx, project, version["id"], version["name"])
    saved = _tracker_row(ctx, project.key, version["id"])
    if saved and saved["status"] == "released" and saved.get("tag") and saved.get("master_sha"):
        if ctx.github is not None:
            merge_back(ctx, project, tracker, version["name"], saved["tag"], run)
        return {"released": True, "status": "released", "tag": saved["tag"], "sha": saved["master_sha"],
                "release_url": saved.get("github_release_url") or ""}
    prior_failure = ctx.db.one("SELECT payload FROM write_audit WHERE ticket=%s AND action='jira-release-check-failed'"
                               " AND run_id=%s ORDER BY id DESC LIMIT 1", (tracker, str(run["id"])))
    if prior_failure:
        return {"released": False, "status": "failed",
                "bugs": (prior_failure["payload"] or {}).get("bugs") or []}
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

    checked_head = str(report.get("head_sha") or "")
    source_sha = str(report.get("source_sha") or checked_head)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", checked_head):
        return _failed_checks(ctx, project, version, tracker, run,
                              [{"command": "resolve checked develop commit", "exit_code": -1,
                                "output": "The release worker did not report a valid checked commit SHA."}])
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", source_sha):
        return _failed_checks(ctx, project, version, tracker, run,
                              [{"command": "resolve release branch source commit", "exit_code": -1,
                                "output": "The release worker did not report a valid source commit SHA."}])
    repo = project.repo_name
    source, target = source_branch(project), target_branch(project)
    if ctx.github is None:
        raise RuntimeError("GitHub App is not connected")
    current_source = ctx.github.branch_sha(repo, source)
    if current_source.lower() != source_sha.lower():
        detail = f"{source} moved during the full check ({source_sha[:12]} checked, {current_source[:12]} now); retrying."
        _set_state(ctx, project.key, version["id"], status="pending", checked_develop_sha=source_sha, gate_detail=detail)
        ctx.jira.comment(tracker, f"Full checks passed but publication stopped: {detail}")
        ctx.events.emit("version_release.check", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], outcome="stale", reason=detail,
                        data={"version_id": version["id"], "checked_sha": source_sha, "current_sha": current_source})
        return {"released": False, "status": "stale", "reason": detail}

    branch = release_branch(project, version["name"], source_sha)
    if not ctx.github.branch_exists(repo, branch):
        ctx.github.create_branch(repo, branch, source)
    current_release_head = ctx.github.branch_sha(repo, branch)
    if current_release_head.lower() != checked_head.lower():
        detail = f"release branch {branch} moved during the full check; retrying."
        _set_state(ctx, project.key, version["id"], status="pending", checked_develop_sha=source_sha,
                   gate_detail=detail)
        return {"released": False, "status": "stale", "reason": detail}

    notes, release_files = _prepare_release_files(ctx, project, repo, branch, snapshot, source_sha,
                                                   report.get("checks", []))
    changed_files = []
    for path, content in release_files.items():
        old = ctx.github.file_content(repo, branch, path)
        if old == content:
            continue
        file_sha = ctx.github.commit_file(repo, branch, path, content,
                                          f"release: update {path} for {version['name']}")
        changed_files.append(path)
        if not file_sha:
            log.info("GitHub did not return a commit SHA while updating release file %s", path)
    release_head = ctx.github.branch_sha(repo, branch)
    _audit(ctx, project, tracker, run, "github-release-files", release_head,
           {"branch": branch, "source_sha": source_sha, "files": list(release_files),
            "changed_files": changed_files})

    previous_merge = _release_audit(ctx, tracker, "github-merge")
    pr = ctx.github.release_pr(repo, branch, target)
    if previous_merge:
        master_sha = str(previous_merge.get("result") or "")
    elif pr and pr.get("merged"):
        master_sha = str(pr.get("merge_commit_sha") or "")
    else:
        if pr and pr.get("state") == "closed":
            pr = ctx.github.reopen_pr(repo, int(pr["number"]))
        if not pr:
            pr = ctx.github.create_pr(repo, branch, target, f"Release {version['name']} ({project.key})",
                                      notes, draft=False)
        ctx.github.enable_merge_commit(repo)
        merged = ctx.github.merge_pr(repo, int(pr["number"]), f"Release {version['name']}", method="merge")
        if not merged.get("merged") or not merged.get("sha"):
            raise RuntimeError(f"release PR #{pr['number']} could not be merged without squash: "
                               f"{merged.get('message') or 'GitHub refused merge'}")
        master_sha = str(merged["sha"])
        pr = {**pr, "merge_commit_sha": master_sha, "merged": True}
    if not master_sha:
        master_sha = ctx.github.branch_sha(repo, target)
    pr = pr or {}
    _audit(ctx, project, tracker, run, "github-merge", master_sha,
           {"source": branch, "develop_source": source, "target": target, "checked_sha": source_sha,
            "release_head_sha": release_head, "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url", ""), "method": "merge"})
    ctx.events.emit("version_release.merge", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="merged", reason=master_sha,
                    data={"source": branch, "target": target, "checked_sha": source_sha,
                          "pr_number": pr.get("number"), "method": "merge"})
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
    release_created = not release
    if release_created:
        release = ctx.github.create_release(repo, tag, version["name"], notes)
    release_url = str(release.get("html_url") or "")
    _audit(ctx, project, tracker, run, "github-release", release_url or tag,
           {"tag": tag, "sha": master_sha, "state": "created" if release_created else "existing"})
    ctx.events.emit("version_release.github_release", ticket=tracker, project=project.key, step="version_release",
                    run_id=str(run["id"]), fence=run["fence"], outcome="created" if release_created else "existing",
                    reason=release_url or tag, data={"tag": tag, "sha": master_sha, "url": release_url})
    # Jira version Released is last among publication steps; retries reconcile the same merge/tag/release.
    latest_version = next((v for v in ctx.jira.versions(project.key) if v["id"] == version["id"]), version)
    if not latest_version.get("released") or not latest_version.get("release_date"):
        ctx.jira.release_version(version["id"])
    # Merge-back runs even when a post-release write below fails; it audits itself, and the scan retries it for
    # any released version without a merge-back record (`reconcile_merge_backs`). Keep the registry write inside
    # this block so a failure there cannot skip merge-back.
    try:
        _set_state(ctx, project.key, version["id"], status="released", checked_develop_sha=source_sha,
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
                         f"Checks passed on {source} commit: {source_sha}\nCommit on {target}: {master_sha}\n"
                         f"Pull request: {pr.get('html_url', '')}\nTag: {tag}\nGitHub Release: {release_url or tag}\n"
                         "Jira version: Released")
        ctx.events.emit("version_release.completed", ticket=tracker, project=project.key, step="version_release",
                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"], node=run["node"],
                        outcome="released", reason=f"{tag} {release_url}",
                        data={"tickets": snapshot["tickets"], "checked_sha": source_sha, "master_sha": master_sha,
                              "tag": tag, "release_url": release_url, "jira_version": "Released"})
    finally:
        merge_back(ctx, project, tracker, version["name"], tag, run)
    return {"released": True, "status": "released", "tag": tag, "sha": master_sha,
            "release_url": release_url}


def _merge_back_recorded(ctx, tracker: str) -> bool:
    return bool(ctx.db.one("SELECT 1 AS done FROM write_audit WHERE ticket=%s AND action IN (%s, %s) LIMIT 1",
                           (tracker, *MERGE_BACK_ACTIONS)))


def _best_effort(what: str, fn, *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - the merge-back outcome is already audited
        log.exception("version release %s failed", what)


def merge_back(ctx, project: Any, tracker: str, version_name: str, tag: str, run: dict) -> None:
    """Create one ready Jira merge-back Task only when the destination has commits missing from the source."""
    if _merge_back_recorded(ctx, tracker):
        return
    repo = project.repo_name
    source, target = source_branch(project), target_branch(project)
    try:
        comparison = ctx.github.compare_branches(repo, source, target)
    except Exception:  # noqa: BLE001 - an unavailable comparison must not invent a merge-back ticket
        log.exception("version release merge-back comparison failed for %s %s", project.key, version_name)
        return
    if int(comparison.get("ahead_by", 0)) <= 0:
        _audit(ctx, project, tracker, run, "github-merge-back", "not-needed",
               {"source": target, "target": source, "tag": tag, "reason": "master has no commits missing from develop"})
        _best_effort("merge-back event", ctx.events.emit, "version_release.merge_back", ticket=tracker,
                     project=project.key, step="version_release", run_id=str(run["id"]), fence=run["fence"],
                     outcome="not_needed", reason="master is already in develop", data={"source": target, "target": source})
        return
    master_sha = ctx.github.branch_sha(repo, target)
    develop_sha = ctx.github.branch_sha(repo, source)
    task = ctx.jira.create_issue(project.key, f"Merge {target} back after {version_name}",
                                 f"Release {tag} is published on {target} at {master_sha}, while {source} is at "
                                 f"{develop_sha}. Merge {target} into {source} and resolve any conflicts.",
                                 issuetype="Task", labels=["aiflow-version-release", "merge-back", "aiflow-ready"])
    _audit(ctx, project, tracker, run, "jira-merge-back-task", task,
           {"source": target, "target": source, "tag": tag, "master_sha": master_sha,
            "develop_sha": develop_sha, "ahead_by": comparison.get("ahead_by")})
    _best_effort("merge-back task transition", ctx.jira.transition_to, task, "To Do")
    _best_effort("merge-back comment", ctx.jira.comment, tracker,
                 f"Master has commits not yet in develop; merge-back ticket {task} created.")
    _best_effort("merge-back event", ctx.events.emit, "version_release.merge_back", ticket=tracker,
                 project=project.key, step="version_release", run_id=str(run["id"]), fence=run["fence"],
                 outcome="ticket_created", reason=task,
                 data={"source": target, "target": source, "master_sha": master_sha, "develop_sha": develop_sha})


def version_identity(project: str, version_id: str) -> str:
    """Stable synthetic run key; never a Jira ticket and safe as a Redis lease identifier."""
    digest = hashlib.sha256(f"{project}:{version_id}".encode()).hexdigest()[:16]
    return f"release:{project}:{digest}"
