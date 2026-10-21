# Versioned v4 releases

AI Flow v4 is released from tags on `master`, so a node installs an immutable version rather than following a
moving branch. Release tags must use `vX.Y.Z` (for example, `v4.1.0`) and point to a commit reachable from
`origin/master`.

## Build and publish

Pushing a valid version tag starts `.github/workflows/release-v4.yml`. It runs `tests/v4` against Redis and
PostgreSQL, builds and signs the release tarball, then installs that exact signed artifact in clean Ubuntu
systemd containers with the control API and two nodes. The nodes must come online, claim a smoke ticket, upgrade
to the candidate, and recover the previous release after an intentionally unhealthy install. The GHCR image is
built and pushed and the GitHub Release is created only after these checks pass. The workflow:

1. Archives the project code, `prompts/`, `deploy/`, `pyproject.toml` and `uv.lock` into
   `aiflow4-vX.Y.Z.tar.gz`.
2. Writes `SHA256SUMS` and signs the tarball as a Sigstore/cosign blob using GitHub Actions OIDC; no long-lived
   signing key is stored in GitHub secrets.
3. Runs the [Ubuntu systemd install smoke test](install.md) against that signed tarball. A failure stops the job
   before GHCR or GitHub Release publication.
4. Builds and pushes `ghcr.io/jooservices/ai-flow/control-api:vX.Y.Z` and signs the image with GitHub Actions OIDC.
5. Creates or updates the matching GitHub Release with notes grouped from conventional commits since the
   previous reachable version tag (or all reachable history for the first versioned release), then attaches the
   tarball, its `.sigstore.json` bundle and `SHA256SUMS`.

Conventional subjects such as `feat(api): add release manifest`, `fix(node): reject invalid signatures` and
`feat!: change the node update protocol` appear under Features, Fixes and Breaking Changes. A commit with a
`BREAKING CHANGE:` body is also listed under Breaking Changes. Other commit subjects are retained under Other.

## Deploy a release

The control host needs the `gh`, `docker`, `cosign`, `git`, `systemctl`, `uv` and `sha256sum` commands, access to
the `jooservices/ai-flow` GitHub Release and GHCR package, and the existing AI Flow control networks. Configure
Docker with GHCR `read:packages` access on the control host when the package is private. Install cosign on the
control host and each node from the [official cosign installation instructions](https://docs.sigstore.dev/cosign/system_config/installation/).
The node also needs Python 3.12 and uv as required by the installer.

Run a specific published release on the control host:

```sh
aiflow4-deploy v4.1.0
```

The deploy script checks that the tag is on `master`, downloads the matching release assets, checks the SHA-256
manifest and cosign identity, verifies the signed GHCR image, and then updates control-api, the control loops
and the control host's node agent. It stores signed assets in
`/srv/aiflow/control/releases/vX.Y.Z/` and atomically updates `CURRENT`. The timer-based auto-deployer selects
the highest published SemVer tag reachable from `master`; it no longer deploys unversioned commits from
`develop`. It rolls back to the previous version when its health checks fail: a previous `vX.Y.Z` is deployed
again with `aiflow4-deploy`; when `CURRENT` is still a commit id from the old develop flow (the first versioned
deploy), the control containers are restarted on the kept `aiflow4/control:<commit>` image and build tree,
`CURRENT` is written back and the control host's node agent returns to `/srv/aiflow/releases/<commit>`. The
critical alert says "rolled back" only when the rollback succeeded; otherwise its subject ends in
"NOT rolled back" and the control plane must be restored by hand.

The image tag is also its version and includes OCI source, version and revision labels. Control containers set
`AIFLOW4_VERSION` from the release tag, so `/healthz`, `/v1/state` and the API metadata report the built version.

## Node download and verification

Node heartbeats receive the control plane's `CURRENT` release through a rolling upgrade: a canary node first,
then waves, with an automatic rollback on a failed health check ([Fleet upgrade](fleet-upgrade.md)). The
authenticated release API exposes:

- `GET /v1/releases/{version}/manifest`: version, SHA-256 digest, and links to the archive, signature bundle and checksum
  file.
- `GET /v1/releases/{version}.tar.gz`: release archive.
- `GET /v1/releases/{version}.tar.gz.sigstore.json`: cosign bundle containing the blob signature and
  verification material.
- `GET /v1/releases/{version}/SHA256SUMS`: release checksum manifest.

The node agent compares the downloaded archive digest to the manifest and runs `cosign verify-blob` before it
starts the installer. Verification requires both the GitHub Actions OIDC issuer and the exact identity
`https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/vX.Y.Z`. The installer checks
the checksum and signature again before extracting files or switching `/srv/aiflow/current`.
Nodes need egress to Sigstore's public verification services so cosign can validate the certificate and
transparency proof in the bundle against its trusted roots.

If the checksum, signature, certificate identity or verifier is invalid or unavailable, installation stops.
The agent sends `POST /v1/nodes/{name}/release-failures` with node authentication; the control API opens a
critical `release_verification` incident for that node and version. A rejected release is not installed.

The installer entry point is:

```sh
aiflow4-node-install <release.tar.gz> <vX.Y.Z> <release.sigstore.json> <sha256>
```

### Nodes still on an older agent

An older agent may extract the installer from a release archive and execute it before checking the archive's
signature. The control API therefore advertises automatic releases only to agents that report
`release_protocol: 1`, which the signed-release agent sends in its heartbeat. Legacy agents receive an empty
release target, and the installer rejects the old two-argument call with a critical incident. Bootstrap these
nodes manually; do not let a legacy agent execute an installer extracted from a not-yet-verified archive.

On the node, install cosign using a trusted administrative process, then stage the tarball, its signature bundle
and `SHA256SUMS` from the matching GitHub Release. Verify the archive before extracting or running its installer:

```sh
VERSION=v4.1.0
TAR="aiflow4-$VERSION.tar.gz"
BUNDLE="$TAR.sigstore.json"
SUMS=SHA256SUMS
cosign verify-blob "$TAR" --bundle "$BUNDLE" \
  --certificate-identity "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/$VERSION" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
sha256sum --check "$SUMS"
SHA256=$(awk -v f="$TAR" '$2 == f || $2 == "*" f {print $1; exit}' "$SUMS")
BOOTSTRAP=$(mktemp -d)
tar -xzf "$TAR" -C "$BOOTSTRAP" deploy/v4/bin/aiflow4-node-install
sudo install -m 0755 "$BOOTSTRAP/deploy/v4/bin/aiflow4-node-install" /usr/local/sbin/aiflow4-node-install
sudo /usr/local/sbin/aiflow4-node-install "$PWD/$TAR" "$VERSION" "$PWD/$BUNDLE" "$SHA256"
rm -rf "$BOOTSTRAP"
```

The manual signature verification establishes trust in the extracted installer; the installer then checks the
checksum and signature again before unpacking the full release or switching `/srv/aiflow/current`. Once the new
agent reports `release_protocol: 1`, later signed releases are eligible for automatic updates.

Do not install a tarball from a branch or commit hash. To make a release, merge the change to `master`, create a
`vX.Y.Z` tag on that commit, then use the corresponding published GitHub Release.
