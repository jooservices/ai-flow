# Command Center node install and repair

## Release installation verification

The tagged release workflow installs the signed candidate tarball in a clean Ubuntu 24.04 systemd environment
before it publishes the GHCR image or GitHub Release. It starts the control API and two node agents, checks
online heartbeats and a node-authorized work claim, then verifies a node can follow an upgrade and that an
unhealthy install restores the previous release. A failed step blocks publication. See [v4 release installation
smoke test](install.md) for the exact CI setup.

When investigating a failed release job, first check the `Install release on Ubuntu systemd control and two
nodes` step. A control API health timeout points to artifact startup or database connectivity; a missing online
node points to join, systemd, or heartbeat handling; an unavailable claim points to node authorization or
project/account configuration. A failed rollback check means the node did not return to its previous release
and healthy agent state. Re-run the tagged workflow after fixing the cause; the release is created only after
all these checks pass.

The Command Center node drawer lets an authenticated owner install missing required tools or queue a repair to
the desired state in `aiflow-control`. The API stores the operation, actor and result in `node_operations`, adds
an entry to `write_audit`, and sends the queued operation to that node's authenticated agent. The agent polls
with its node token; it never accepts a shell command from the browser or API.

## Console roles

Node mutations require the signed `aiflow4_session` cookie. `AIFLOW4_CONSOLE_SESSION_SECRET` must be set to a
random value of at least 32 bytes. The existing console login feature issues an HS256 JWT signed with that
secret and claims `sub` (user), `role` (`owner` or `lead`) and `exp` (Unix time). Use
`aiflow.v4.control.console_auth.issue_session` to create the same token. Set the cookie as HttpOnly, Secure,
SameSite=Strict and Path `/`. The control API ignores user and role headers.

Only `owner` can install, repair or use the per-node join-command action. The Join a node panel authorizes token
creation with the configured `AIFLOW4_JOIN_TOKEN`. `owner` and `lead` can read the per-node operation log.
Missing, invalid and expired sessions fail closed; a lead write attempt is rejected and audited.

## Desired state

`versions.lock` pins versions under `tools`. The toolchain mapping in `aiflow/v4/core/toolchains.py` accepts
the `claude_code` alias for Claude Code and the normal tool names for Codex, Grok, OpenCode, Docker, PHP,
Composer, WP-CLI, Git, Node, uv, GitHub CLI and Python. The `stack` of every project served by a node adds its
required tools (for example `php-8.3` requires PHP, Composer and WP-CLI). AI CLI repair requires an exact lock
entry and never authorizes or signs an AI account in.

Node files can add explicit tools, OS packages and systemd services to the desired state:

```yaml
name: app-node
projects: [WEB]
tools: [docker]
packages: [ca-certificates, curl, unzip]
services:
  - name: app-api
    unit: app-api.service
    healthcheck: http://127.0.0.1:8080/healthz
```

Repair starts each declared service with `systemctl enable --now`, confirms it is active, and requires any
configured HTTP health check to return a 2xx response. A service with no URL uses its active systemd state as
its health check. A successful operation reports installed tool versions and green service checks.

## Execution and safety

The supported agent operations are `install` and `repair`. Tool names, version pins, package names and service
units come from the control repo; request bodies cannot supply a command. AI CLI plans include Node.js before
the fixed npm package install; the agent installs the pinned Node.js distribution if Node or npm is missing.
AI CLIs use fixed npm package names with versions from `versions.lock`. The agent also installs `uv`, Python,
GitHub CLI, Composer, WP-CLI and `codex-code-mode-host` from fixed recipes. GitHub release asset digests,
Node.js checksums and the official Composer installer signature are verified before installation. Python is installed through the
node's pinned uv release; the Codex host is selected from the same release as the pinned Codex CLI, and its
installed version is recorded for later reconciliation.

Project stacks are prepared automatically and checked against their `versions.lock` pins. See
[Project toolchains on every node](project-toolchains.md) for the stack format, automatic host installation,
project admission and retry behavior.

Operating system components reuse the bash CLI of
[`jooservices/server-installer`](https://github.com/jooservices/server-installer) (TESTFLOW-68), pinned to tag
`v1.1.0` at the fixed path `/opt/server-installer/v1.1.0/bin/server-installer`. The agent runs it without a
shell, with an argv built from a fixed map; any other component name is rejected:

| Component | Command |
| --- | --- |
| `packages`, `git` | `server-installer apply --modules packages`; a temporary `packages` module passes the base and declared list through the installer's package-manager helpers |
| `docker` | `server-installer apply --modules docker` |
| `php` | `SI_PHP_VERSION=<MAJOR.MINOR from versions.lock> server-installer apply --modules php` |

Inherited `SI_*` variables are removed before the call. Before the first OS component operation, the agent
downloads the pinned `v1.1.0` source archive from the official GitHub repository, safely extracts it, and installs
the CLI at the fixed path. No per-node manual clone is required. The generated package module checks and verifies
the full declared list through the server-installer's distro-aware package manager, including on Debian/Ubuntu and
RHEL-family hosts.

An unavailable or invalid pinned installer archive fails the operation. Other artifact downloads use fixed vendor
URLs, locked versions (or the vendor's latest stable release when no supporting-tool pin is present), and a
checksum before writing to `/usr/local/bin`. Arbitrary commands and arbitrary artifact URLs are never accepted
from the API or control repo.

`GET /v1/nodes/{name}/operations/next` is node-token protected. The agent claims one queued operation and posts
its verified result to `/result`. The Command Center log is `GET /v1/nodes/{name}/operations`, available only
to owner and lead sessions.

## Nodes without an agent

The owner can create a one-line join command from the Command Center. The control API advertises the URL from
`AIFLOW4_JOIN_CONTROL_URL`, which must be an HTTP(S) address reachable from the joining machine. The API rejects
missing, loopback, link-local, unspecified and multicast addresses before creating a token; `AIFLOW4_PUBLIC_URL`
is used for console links and is not used for node enrollment. Configure the join URL in the Compose environment
on the control plane, such as `AIFLOW4_JOIN_CONTROL_URL=https://control.example.test` in the Compose `.env` file.
The copied command is `curl -fsSL https://<control>/v1/install.sh | sudo sh -s -- --token T`. The bootstrap
validates the bound node, role, project, and stack before changing the host, then redeems the token and installs
the pinned server-installer modules, `aiflow-worker`, project toolchain, AI CLIs, and signed AI Flow release as
described in [Node join](node-join.md). The installer and node agent verify release checksums and signatures.

The random join token expires after five minutes, is accepted once, and is stored in PostgreSQL only as a
SHA-256 hash with its node, role, and project grant. The join command never accepts SSH passwords or AI account
credentials. VM creation remains the separate TESTFLOW-55 flow.

AI account authorization remains the TESTFLOW-76 flow; node install and repair do not read or change account
credentials.

Tests: `tests/v4/test_node_operations.py` and `tests/v4/test_console.py`.
