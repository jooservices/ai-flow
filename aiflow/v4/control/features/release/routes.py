"""Routes of the release feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
from pathlib import Path
from types import SimpleNamespace

from .bodies import ReleaseFailureBody, WorktreeSweepBody
from fastapi import Depends, HTTPException
from fastapi.responses import (FileResponse)
from ... import (rollout)


def install(app, ctx, deps) -> SimpleNamespace:
    def _releases():
        from ...api import RELEASES  # noqa: PLC0415 - read when a route runs, so tests may patch it
        return RELEASES

    node_auth = deps.node_auth
    release_reader = deps.release_reader
    # --- releases: nodes follow a tagged, signed release written by aiflow4-deploy ---------------------------
    def current_release(node: str = "") -> str:
        # During a canary, only the selected node sees the candidate. The file is written by the host updater
        # and shared read-only with control containers; CURRENT remains the fleet-wide stable release.
        canary = rollout.canary_override(_releases())
        if node and canary.get("node") == node and canary.get("target"):
            return str(canary["target"])
        return rollout.read_current(_releases())

    def fleet_release(node: str, reported: str) -> str:
        """CURRENT moves the fleet through a rollout: the canary and wave members get it, other nodes keep the
        previous release, and a halted rollout sends its members back."""
        canary = rollout.canary_override(_releases())
        if canary.get("node") == node and canary.get("target"):
            return str(canary["target"])
        return rollout.desired_release(ctx, node, reported, rollout.read_current(_releases()))

    def tagged_release(version: str) -> Path:
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
            raise HTTPException(400, "bad release version")
        return _releases() / version

    def release_assets(version: str) -> tuple[Path, Path, Path, str]:
        directory = tagged_release(version)
        tar_name = f"aiflow4-{version}.tar.gz"
        tarball = directory / tar_name
        bundle = directory / f"{tar_name}.sigstore.json"
        sums = directory / "SHA256SUMS"
        if not all(path.is_file() for path in (tarball, bundle, sums)):
            raise HTTPException(404, "no such signed release")
        checksum = re.compile(rf"^([0-9a-f]{{64}})  {re.escape(tar_name)}$")
        matches = [m.group(1) for line in sums.read_text().splitlines() if (m := checksum.fullmatch(line))]
        if len(matches) != 1:
            raise HTTPException(404, "release checksum manifest is incomplete")
        return tarball, bundle, sums, matches[0]

    @app.get("/v1/releases/{version}/manifest")
    def release_manifest(version: str, _node: str = Depends(node_auth)) -> dict:
        _, _, _, checksum = release_assets(version)
        base = f"/v1/releases/{version}"
        tar_name = f"aiflow4-{version}.tar.gz"
        return {"version": version, "sha256": checksum, "tarball": f"{base}.tar.gz",
                "signature": f"{base}.tar.gz.sigstore.json", "checksums": f"{base}/SHA256SUMS",
                "filename": tar_name}

    @app.get("/v1/releases/{version}/SHA256SUMS")
    def release_checksums(version: str, _node: str = Depends(node_auth)) -> FileResponse:
        _, _, sums, _ = release_assets(version)
        return FileResponse(sums, media_type="text/plain", filename="SHA256SUMS")

    @app.get("/v1/releases/{version}.tar.gz.sigstore.json")
    def release_signature(version: str, _node: str = Depends(release_reader)) -> FileResponse:
        _, bundle, _, _ = release_assets(version)
        return FileResponse(bundle, media_type="application/json", filename=f"aiflow4-{version}.tar.gz.sigstore.json")

    @app.get("/v1/releases/{sha}.tar.gz")
    def release_file(sha: str, node: str = Depends(release_reader)) -> FileResponse:
        if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", sha):
            f, _, _, _ = release_assets(sha)
        elif re.fullmatch(r"[0-9a-f]{7,40}", sha):  # old, already-installed releases remain downloadable
            f = _releases() / f"aiflow4-{sha}.tar.gz"
        else:
            raise HTTPException(400, "bad release id")
        if not f.is_file():
            raise HTTPException(404, "no such release")
        return FileResponse(f, media_type="application/gzip")

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
        """Which of a node's worktree directories can go: every directory of a ticket that is Done, and the
        per-run checkouts (intake, impact, triage) of a ticket that has no run in progress."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        remove: list[str] = []
        for entry in body.dirs[:2000]:
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
        return {"remove": remove}

    return SimpleNamespace(current_release=current_release, fleet_release=fleet_release, tagged_release=tagged_release, release_assets=release_assets, release_manifest=release_manifest, release_checksums=release_checksums, release_signature=release_signature, release_file=release_file, release_failure=release_failure, worktrees_sweep=worktrees_sweep)
