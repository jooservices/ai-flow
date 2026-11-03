# Release routes structure (JAIF-553)

`install(app, ctx, deps)` in `aiflow/v4/control/features/release/routes.py` is a short function: it defines the release helpers other parts of the app use (`current_release`, `fleet_release`, `tagged_release`, `release_assets`) and installs two route groups in a fixed order. `tests/v4/test_release_install_parity.py` records every registration (method, path, handler, signature) of the unchanged code and compares it after the split.

| Part | Module | Owns |
|---|---|---|
| Files on disk | `release_files.py` | `tagged_release` (version check, directory) and `release_assets` (signed files and the single checksum line) |
| Downloads | `routes_assets.py` | the manifest, the checksum list, the signature bundle and the tarball of a release |
| Node reports | `routes_nodes.py` | a node rejecting a release, and which worktree directories of a node can be removed (`removable_worktrees`) |

## Routes

- `GET /v1/releases/{version}/manifest`, `GET /v1/releases/{version}/SHA256SUMS`, `GET /v1/releases/{version}.tar.gz.sigstore.json` and `GET /v1/releases/{sha}.tar.gz` (downloads, in this order)
- `POST /v1/nodes/{name}/release-failures` and `POST /v1/nodes/{name}/worktrees/sweep` (node reports)

To add a release route, put it in the part that owns it; keep the registration order.
