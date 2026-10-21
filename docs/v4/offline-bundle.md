# Offline node bundles and Debian packages

`aiflow4-offline-bundle` creates a checksummed, architecture-specific installation bundle from a published v4
release, its Docker image, and the `versions.lock` in the matching `aiflow-control` checkout. Run the builder on a
connected Linux host of the target architecture: npm and Python wheels can contain architecture-specific files.
The builder accepts amd64 and arm64. Its Python wheel target is manylinux 2.28, which runs on Ubuntu 22.04 and
24.04 and Debian 12. The two service packages are architecture-independent `.deb` files, so the same packages
work on both architectures and all three distributions.

The lock must contain exact pins for Claude Code (`claude_code`, `claude-code` or `claude`), Codex, OpenCode,
Node, uv and Python 3.12. Claude Code, Codex, OpenCode and their transitive npm packages are installed at the
locked versions. The matching `codex-code-mode-host`, Node runtime, uv-managed Python, AI Flow wheel, and all
locked Python dependency wheels are staged too. Grok is omitted by default; `--include-grok` adds it and requires
a `tools.grok` pin. The bundle also contains the release tarball, Docker image archive, lock snapshot, both `.deb`
packages, `SHA256SUMS` covering every bundle file, and a sidecar checksum for the bundle archive.

## Build

The image archive must contain the exact release image tag. Save it on the build host, then build from this
worktree using the `versions.lock` that corresponds to the intended control-repo revision:

```sh
VERSION=v4.2.0
IMAGE=ghcr.io/jooservices/ai-flow/control-api:$VERSION
docker save --output control-image.tar "$IMAGE"
deploy/v4/bin/aiflow4-offline-bundle build \
  --version "$VERSION" \
  --release "aiflow4-$VERSION.tar.gz" \
  --image control-image.tar \
  --image-ref "$IMAGE" \
  --versions-lock /path/to/aiflow-control/versions.lock \
  --output "aiflow4-offline-$VERSION-$(dpkg --print-architecture).tar.gz"
```

The builder verifies the Node.js download checksum and image tag/architecture, pins every tool from the lock,
and fails when a required wheel is not available for Python 3.12 and the target architecture. Run it once on an
amd64 host and once on an arm64 host for both bundle variants. For an optional Grok CLI, add `--include-grok`.
The output `.sha256` file can be checked before transferring the archive. After extraction, the included
`SHA256SUMS` independently verifies the release, image, wheels, CLIs, runtime files and Debian packages.
The connected build host needs the locked `uv`, `python3-pip`, `dpkg-deb`, `curl`, `tar`, `xz` and the Docker CLI.

The Debian packages are `aiflow4-node-agent` and `aiflow4-worker`. They install the systemd agent and worker
template units, create the `aiflow-worker` system user, and prepare `/etc/aiflow` and `/srv/aiflow` directories.
They depend only on `systemd` and `passwd`, already present on supported Ubuntu Server and Debian installations.
They do not start the agent before the release runtime and its offline dependencies have been installed.

## Install without internet

The target needs Ubuntu 22.04/24.04 or Debian 12 on the bundle architecture, systemd, Docker Engine, Git, curl,
and an existing `/etc/aiflow/node.env` plus node token. Docker and Git can be installed from local OS media or
an internal apt mirror. The node must be able to reach the AI Flow control API on its private network so its
heartbeat can be checked; no public internet is used during bundle installation. Configure the node with the
same protected environment file and token path used by the normal node installer. The install command never
reads or creates provider login credentials.

Copy and verify the archive, extract it, then run the included installer as root:

```sh
sha256sum --check aiflow4-offline-v4.2.0-amd64.tar.gz.sha256
tar -xzf aiflow4-offline-v4.2.0-amd64.tar.gz
sudo python3 aiflow4-offline-v4.2.0-amd64/offline_bundle.py install \
  aiflow4-offline-v4.2.0-amd64
```

The installer checks every `SHA256SUMS` entry before changing the host, installs the two `.deb` packages, creates
the Python 3.12 virtual environment from bundled wheels with uv offline mode enabled, installs the release,
loads the included Docker image, and starts `aiflow4-node-agent`. It waits for the control API to report the node
online at the installed release with no missing required tools. If accounts are assigned to the node, it also
waits for an active `aiflow4-worker@<account>.service`. Workers can be running while their provider accounts are
still signed out; TESTFLOW-76 handles sign-in separately. Check `systemctl status aiflow4-node-agent` and the
node's `missing_tools` field in `/v1/state` for the green preflight result.

All packages are built with `Architecture: all` and use standard systemd paths. This keeps the `.deb` package
format compatible with Debian 12 as well as the two supported Ubuntu releases; the bundle itself remains
architecture-specific because its Python runtime, wheels, and AI CLI dependencies are platform-specific.
