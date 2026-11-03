"""Routes of the release feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from pathlib import Path
from types import SimpleNamespace

from ... import rollout
from . import release_files, routes_assets, routes_nodes


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
        return release_files.tagged_release(_releases, version)

    def release_assets(version: str) -> tuple[Path, Path, Path, str]:
        return release_files.release_assets(_releases, version)

    downloads = routes_assets.install_release_asset_routes(app, release_assets, _releases, node_auth, release_reader)
    node_routes = routes_nodes.install_node_release_routes(app, ctx, node_auth)
    return SimpleNamespace(current_release=current_release, fleet_release=fleet_release, tagged_release=tagged_release,
                           release_assets=release_assets, release_manifest=downloads.release_manifest,
                           release_checksums=downloads.release_checksums, release_signature=downloads.release_signature,
                           release_file=downloads.release_file, release_failure=node_routes.release_failure,
                           worktrees_sweep=node_routes.worktrees_sweep)
