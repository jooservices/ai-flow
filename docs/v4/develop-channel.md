# Develop channel (lab only)

Production hosts install signed `vX.Y.Z` releases from `master` (docs/v4/release.md). While AI Flow is still in
development there is no release: only `develop` exists. A lab host can follow `develop` instead, by opting in
explicitly:

- control-plane host: `AIFLOW4_CHANNEL=develop` in `/etc/aiflow/deploy.env`. `aiflow4-autodeploy` then hands over
  to `aiflow4-autodeploy-develop`, which waits for CI, stages `origin/develop`, and sends it to one canary node.
  It watches completed-run infrastructure failures using the same configurable time, run-count, and rate
  thresholds as signed releases. A rejected canary leaves `CURRENT` and all other nodes on the old commit;
  a stable canary promotes through `aiflow4-deploy-develop`, which rebuilds the control image, restarts the
  control containers, publishes `aiflow4-<sha>.tar.gz`, and sets `CURRENT` to the new commit id.
  Control-plane health checks still roll back an unhealthy promotion.
- every node: `AIFLOW4_CHANNEL=develop` in `/etc/aiflow/node.env`. The agent downloads the control plane's build
  named by commit id and installs it with `aiflow4-develop-install`, the installer it already has (never one
  taken from the new archive).

Without the setting every script exits with code 3 and a node ignores unversioned builds, so the signed-release
path is unchanged. Develop builds are NOT signed: use this channel only on a lab whose control plane you trust.
