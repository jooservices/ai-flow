# v4 release installation smoke test

Every tagged v4 release must pass the install smoke test before AI Flow publishes either the GHCR control image
or the GitHub Release. The release workflow packages and signs the tarball first, then installs those exact
assets in fresh Ubuntu 24.04 containers running systemd. PostgreSQL with pgvector and Redis run as isolated CI
services, and the control API, two node agents, and their systemd units run in the Ubuntu containers.

The smoke test installs the control API from the candidate tarball, creates a small CI-only control repo, and
joins two nodes through the normal one-use join-token API. It checks that both agents report online heartbeats,
then submits a real development claim using one node's token. The worker units are disabled in this fixture so
the claim remains available for the smoke check.

For an ordinary release, the nodes start from the preceding published signed release. For the first tagged
release without a published predecessor, they start from the tagged commit's parent as the legacy develop
baseline. One node then upgrades to the control plane's desired release and must report the candidate version. The
other node first receives an intentionally unhealthy candidate tree: the signed installer must restore its
previous symlink and the agent must resume heartbeating. The test removes that fixture and installs the real
candidate on the second node before it passes.

The CI implementation is in `deploy/v4/ci/install-smoke.sh` and
`deploy/v4/ci/systemd.Dockerfile`. The release job also runs the normal v4 tests and only builds or pushes the
GHCR image and creates or updates a GitHub Release after the install smoke step succeeds. A smoke failure leaves
the tag unpublished and fails the release job.

## Installing a published release

The control host installs a version with:

```sh
aiflow4-deploy vX.Y.Z
```

The deploy helper downloads the tagged archive, checksum and Sigstore bundle, verifies their checksum and
workflow identity, then updates the control services and the control host's node agent. Join new nodes using the
one-use command in Command Center, or install a signed release on an existing node with
`aiflow4-node-install <archive> <version> <bundle> <sha256>`. See [Node operations](node-operations.md) for
health checks and recovery steps, and [Versioned releases](release.md) for artifact verification and release
rollback behavior.
