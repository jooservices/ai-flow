"""What a node reports about releases and what the control plane tells it to clean up."""
import re
from types import SimpleNamespace
from typing import Callable

from fastapi import Depends, HTTPException

from ... import rollout
from .bodies import ReleaseFailureBody, WorktreeSweepBody


def removable_worktrees(ctx, dirs: list[str]) -> list[str]:
    """Every directory of a ticket that is Done, and the per-run checkouts (intake, impact, triage) of a ticket that
    has no run in progress."""
    remove: list[str] = []
    for entry in dirs[:2000]:
        project, _, directory = entry.partition("/")
        m = re.match(r"([A-Z][A-Z0-9]*-\d+)(.*)$", directory)
        if not m or not project or "/" in directory or ".." in entry:
            continue
        key, rest = m.group(1), m.group(2)
        ticket = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))
        running = ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND status='running' LIMIT 1", (key,))
        per_run = bool(re.fullmatch(r"--(intake|impact|triage-.+)", rest))
        if running:
            continue
        if per_run or (ticket and ticket["status"] == "Done"):
            remove.append(entry)
    return remove


def install_node_release_routes(app, ctx, node_auth: Callable) -> SimpleNamespace:
    @app.post("/v1/nodes/{name}/release-failures")
    def release_failure(name: str, body: ReleaseFailureBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", body.release_version):
            raise HTTPException(400, "bad release version")
        detail = body.detail.strip()[:1000] or "release signature or checksum verification failed"
        title = f"node {node} rejected release {body.release_version}"
        from ...incidents import open_incident
        opened = open_incident(ctx, "release_verification", f"{node}:{body.release_version}", "critical", title, detail)
        ctx.events.emit("node.release_rejected", node=node, release_version=body.release_version, detail=detail)
        rollout.release_rejected(ctx, node, body.release_version, detail)
        return {"recorded": True, "opened": opened}

    @app.post("/v1/nodes/{name}/worktrees/sweep")
    def worktrees_sweep(name: str, body: WorktreeSweepBody, node: str = Depends(node_auth)) -> dict:
        """Which of a node's worktree directories can go (see `removable_worktrees`)."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        return {"remove": removable_worktrees(ctx, body.dirs)}
    return SimpleNamespace(release_failure=release_failure, worktrees_sweep=worktrees_sweep)
