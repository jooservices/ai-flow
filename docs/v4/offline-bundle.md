# Offline v4 node bundle and Debian packages

`aiflow4-offline build` creates a bundle for one target OS and CPU architecture. Build it on an online machine
running the same Ubuntu 22.04/24.04 or Debian 12 release and architecture as the disconnected node. Repeat the
build for each target in the Ubuntu/Debian amd64/arm64 matrix. The builder needs Docker, Node.js/npm, the pinned
`uv`, `dpkg-deb`, root access for downloading the complete local apt dependency closure, and access to GitHub,
npm, nodejs.org, Ubuntu/Debian apt mirrors, and the Python base-image registry used by the release Dockerfile.

The versions lock comes from the matching `aiflow-control` checkout. Claude Code, Codex and OpenCode must have
exact `MAJOR.MINOR.PATCH` pins there. The optional Grok CLI is included with `--include-grok` and also requires
an exact lock pin. Codex's code-mode host is fetched from the same checksummed Codex release. The builder checks
the installed package versions, Node.js release checksum, GitHub asset digests, image architecture and image
version label before writing the bundle.

```sh
uv run --frozen python deploy/v4/bin/aiflow4-offline build \
  --version v4.1.0 \
  --release dist/aiflow4-v4.1.0.tar.gz \
  --versions-lock /path/to/aiflow-control/versions.lock \
  --output dist/offline
```

Use `--include-grok` when the node's accounts need Grok. The builder creates the control image from the release
archive for the target architecture; `--image` selects the local image tag to save, but its OCI version label
must still equal the release tag.
The resulting target-specific tarball contains:

- the release archive and its Python 3.12 runtime wheelhouse, including the AI Flow wheel;
- the matching control image saved for Docker, and pinned Node.js plus CLI installations;
- separate `aiflow4-node-agent` and `aiflow4-worker` architecture-independent `.deb` packages, with their
  systemd units, the `aiflow-worker` system user and `/srv/aiflow` directories;
- offline apt packages for `git`, Docker, CA certificates, user management and `setpriv`, plus `versions.lock`, `manifest.json` and
  `SHA256SUMS` covering every bundle component.

The two `.deb` files can also be published to an internal apt repository. They depend on the usual systemd,
git, Docker and CA certificate packages. The bundle includes those OS packages and their dependency closure so
it can be installed without an apt mirror. Deb packages contain the service units; they do not carry AI Flow's
Python runtime, which is installed from the same release and wheelhouse in the bundle.

## Install on a host without public internet access

The host needs Ubuntu 22.04/24.04 or Debian 12, systemd, `dpkg`, `apt`, and a route to the private control API.
It does not need a host Python interpreter: the installer shell bootstrap verifies the bundle checksums and starts
the bundled Python 3.12 runtime. The control API must be reachable over HTTPS with a certificate and hostname
trusted by the host's CA store. Install any private CA certificate into that trust store before running the bundle.
Create a one-use node join token in Command Center while the control API is reachable, place it in a root-readable
file on the target host, then transfer the bundle and token through the site's approved offline media. The
installer has no calls to public package, image or release services; its only network requests are to the
configured control API to inspect and redeem that token.

```sh
(cd /media/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64 && sha256sum --check SHA256SUMS)
sudo /media/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64/installer/aiflow4-offline \
  verify /media/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64
sudo /media/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64/installer/aiflow4-offline \
  install /media/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64 \
  --control-url https://control.internal \
  --join-token-file /root/aiflow-join-token
```

`install` verifies the complete file inventory and checksums before changing the host, installs the bundled apt
and Debian packages without downloads, loads the image, builds the release venv from local wheels, installs the
pinned CLIs and configures the node. It enables and starts `aiflow4-node-agent`; the agent manages one
`aiflow4-worker@<account>` service for every account assigned to the joined node. This does not sign any AI
account in. TESTFLOW-76 owns that step.

After installation, check Command Center for the node's online heartbeat and green tool preflight, and confirm
the agent and assigned worker units with `systemctl status aiflow4-node-agent` and
`systemctl status 'aiflow4-worker@<account>'`. The node needs only internal control-plane connectivity to report
healthy. AI-provider credentials can be added later.

## Offline installation smoke test

`deploy/v4/ci/offline-install-smoke.sh` runs a real install on an Ubuntu 24.04 systemd container. It builds the
test image before starting the target, then places the target on an internal-only Docker network with only the
supplied control container attached. The installer uses the bundle's `.deb` files, image and wheels; after it
returns, the smoke checks that the agent and at least one assigned worker unit are active and queries the live
control API for a fresh online heartbeat and green preflight.

Use a test control container with the same release available, a one-use join token for a node assigned at least
one account, and a project using a bundled Python or Node stack. The account does not need signed-in AI
credentials. The control container must have a certificate trusted by the target, or pass its private CA file as
the optional fifth argument. Run the smoke from a Docker host that can run privileged systemd containers:

```sh
bash deploy/v4/ci/offline-install-smoke.sh \
  /path/to/aiflow4-offline-v4.1.0-ubuntu-24.04-amd64 \
  aiflow4-test-control-api https://control-smoke.internal:8080 \
  /path/to/one-use-join-token /path/to/private-control-ca.crt
```

The same check can run from pytest when those inputs are provided through
`AIFLOW4_OFFLINE_SMOKE_BUNDLE`, `AIFLOW4_OFFLINE_SMOKE_CONTROL_CONTAINER`,
`AIFLOW4_OFFLINE_SMOKE_CONTROL_URL` and `AIFLOW4_OFFLINE_SMOKE_JOIN_TOKEN_FILE`; set
`AIFLOW4_OFFLINE_SMOKE_CA_CERTIFICATE` when the control API uses a private CA. The test target has no public
network route during installation, while the control API remains reachable over the private Docker network.

## Clean-target apt closure

The builder resolves the apt packages against an empty dpkg status (`Dir::State::status`), so a package that is
already installed on the build machine is still downloaded; `--reinstall` is not used. It then reads each bundled
`.deb` (`dpkg-deb --field`) and fails the build if any `Depends`/`Pre-Depends` group has no provider among the
bundled packages. A bundle that finishes building therefore installs on a bare image with `apt-get --no-download`.

## Supported project stacks

The bundle ships the Python 3.12 and Node.js 24 runtimes and the AI CLIs. The tools of the `php-8.3` stack (PHP,
Composer, WP-CLI) and the `playwright` stack are not bundled, and the node agent would try to download them from
public registries. `install` therefore reads the joining project's stacks while inspecting the join token and
refuses a project with any other stack, before redeeming the token, so the token stays valid for an online install.

## Install success means a healthy node

After the systemd units are active, `install` polls the node-authenticated `GET /v1/nodes/<name>/health`. It
returns success only when the node is `online`, its last heartbeat is at most 60 seconds old and
`preflight_ok` is true (no required tool missing or off its `versions.lock` pin, and every test service a project
declares — PostgreSQL, Redis — reported healthy; the bundle carries their images in `image/services.tar` and the
installer loads them, because the node agent cannot pull on an offline host). Otherwise it exits with an
error that names the missing tools, or says that no healthy heartbeat arrived.

The bundle checksum manifest detects damaged or incomplete transfers. Transfer the checksum file through the
same trusted administrative channel as the bundle; SHA-256 by itself does not authenticate who built it.
