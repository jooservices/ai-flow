# v4 release installation smoke tests

Every tagged v4 release must pass the clean-install acceptance test and the fleet rollout smoke before AI Flow publishes
either the GHCR control image or the GitHub Release. The release workflow packages and signs the tarball
first. `clean-install-containers.sh` installs those exact candidate assets in fresh Ubuntu 24.04 systemd containers
through the documented `aiflow4-control-install` command and `/v1/install.sh` join bootstrap; see
[Clean-install acceptance](clean-install-acceptance.md). PostgreSQL with pgvector and Redis run as isolated CI
services.

The supplementary fleet smoke in `install-smoke.sh` sets up the control API and `control-loops` from the same
candidate tarball, creates a small CI-only control repo, and joins two nodes through the one-use join-token API. It
checks that both agents report online heartbeats, then submits a real development claim using one node's token.
The worker units are disabled in this fixture so the claim remains available for the smoke check. The rollout runs
with a 10 s soak, one node per wave and `smoke-node-a` as the canary, so it finishes within the job.

For an ordinary release, the nodes start from the preceding published signed release, which control also serves:

1. Canary upgrade: `smoke-node-a` is the canary. It installs the candidate itself and passes its soak. `smoke-node-b` is
   then assigned the candidate in wave 1.
2. `smoke-node-b` installs an intentionally unhealthy candidate tree. The signed installer must restore the
   previous symlink, exit 6, and report the failure to control; the agent must resume heartbeating.
3. Control must show what the report caused in `GET /v1/state`: an open `release_verification` incident for
   `smoke-node-b:<version>`, the rollout `halted` with `failed_node` `smoke-node-b` and a "rejected the release"
   reason, and an open `fleet_rollout` incident. The canary must go back to the previous release, and the
   rollout must show `rollback: succeeded`, both members `rolled_back`, and a `fleet_rollback` incident.
4. The test resumes as the runbook in [Node operations](node-operations.md) describes. It removes the broken tree,
   points `CURRENT` at the previous release (that rollout completes at once), then at the candidate again. A new
   rollout must bring both nodes to the candidate and complete.

The first tagged release has no published predecessor. Its nodes start from the tagged commit's parent as the
legacy develop baseline, an unversioned build. Control has no previous fleet release, so the candidate's rollout
is complete at once and there is nothing to halt. The test still requires the failed install's
`release_verification` incident, then installs the real candidate on the second node by hand.

The fleet rollout implementation is in `deploy/v4/ci/install-smoke.sh` and `deploy/v4/ci/systemd.Dockerfile`.
The release job only builds or pushes the GHCR image and creates or updates a GitHub Release after both candidate
smokes succeed. A failure leaves the tag unpublished and fails the release job.

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
