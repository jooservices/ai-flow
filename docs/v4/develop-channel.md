# Develop channel (lab only)

Production hosts install signed `vX.Y.Z` releases from `master` (docs/v4/release.md). While AI Flow is still in
development there is no release: only `develop` exists. A lab host can follow `develop` instead, by opting in
explicitly:

- control-plane host: `AIFLOW4_CHANNEL=develop` in `/etc/aiflow/deploy.env`. `aiflow4-autodeploy` then hands over
  to `aiflow4-autodeploy-develop`, which deploys `origin/develop` once its CI passed (and rolls back when the
  health checks fail) with `aiflow4-deploy-develop`: control image built from the commit, control containers
  restarted, node tarball published as `aiflow4-<sha>.tar.gz`, `CURRENT` set to the commit id.
- every node: `AIFLOW4_CHANNEL=develop` in `/etc/aiflow/node.env`. The agent downloads the control plane's build
  named by commit id and installs it with `aiflow4-develop-install`, the installer it already has (never one
  taken from the new archive).

Without the setting every script exits with code 3 and a node ignores unversioned builds, so the signed-release
path is unchanged. Develop builds are NOT signed: use this channel only on a lab whose control plane you trust.
