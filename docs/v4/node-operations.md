# Command Center node install and repair

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

Only `owner` can install, repair or create a join command. `owner` and `lead` can read the per-node operation
log. Missing, invalid and expired sessions fail closed; a lead write attempt is rejected and audited.

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
units come from the control repo; request bodies cannot supply a command. AI CLIs use fixed npm package names
with versions from `versions.lock`.

Operating system components reuse the bash CLI of
[`jooservices/server-installer`](https://github.com/jooservices/server-installer) (TESTFLOW-68), pinned to tag
`v1.1.0` at the fixed path `/opt/server-installer/v1.1.0/bin/server-installer`. The agent runs it without a
shell, with an argv built from a fixed map; any other component name is rejected:

| Component | Command |
| --- | --- |
| `packages`, `git` | `server-installer apply --modules packages` (base packages: zip, unzip, nano, git) |
| `docker` | `server-installer apply --modules docker` |
| `php` | `SI_PHP_VERSION=<MAJOR.MINOR from versions.lock> server-installer apply --modules php` |

Inherited `SI_*` variables are removed before the call. After the `packages` module, every package listed in the
node file is checked with `dpkg-query`; a package the module does not provide fails the operation instead of
falling back to `apt`. Install the installer on each node (as root) before using these components:

```bash
git clone --depth 1 --branch v1.1.0 https://github.com/jooservices/server-installer /opt/server-installer/v1.1.0
```

A missing installer fails the operation with that path in the message. Tools with neither a server-installer
module nor a fixed npm recipe (`uv`, `gh`, `node`, `composer`, `wp`, `python3`) are not installed by the agent;
repair reports them as not in the install whitelist, and they belong in the node's base image or a
server-installer profile.

`GET /v1/nodes/{name}/operations/next` is node-token protected. The agent claims one queued operation and posts
its verified result to `/result`. The Command Center log is `GET /v1/nodes/{name}/operations`, available only
to owner and lead sessions.

## Nodes without an agent

The owner can create a one-line join command from the Command Center. It works on a machine where AI Flow is
not installed yet: `printf '%s\n' '<script>' | sudo bash -s`. The script reaches root on stdin, so the token is
never a process argument. It:

1. downloads the current signed release (`/srv/aiflow/control/releases/CURRENT`) and its sigstore bundle from
   the control API, authenticated with the join token;
2. checks the SHA-256 from the release `SHA256SUMS` and the cosign signature of the `release-v4.yml` workflow
   for that tag;
3. writes `/etc/aiflow/node.env` (control URL, node name, one slot) only if the file does not exist;
4. installs the release with that release's own `deploy/v4/bin/aiflow4-node-install`;
5. runs `/srv/aiflow/current/.venv/bin/aiflow4 node join --join-token-stdin` and restarts
   `aiflow4-node-agent`.

The machine needs `curl`, `cosign`, `uv` and Python 3.12, which `aiflow4-node-install` already requires. With
no published signed release the API refuses to create the command (409) and audits the refusal.

The random join token expires after 10 minutes, is accepted once for the requested node name and is stored in
PostgreSQL only as a SHA-256 hash. Until it is used it also authorizes downloading the release tarball and
signature; after the join only the node token does. The owner SSH password is neither requested nor stored. VM
creation remains the separate TESTFLOW-55 flow.

AI account authorization remains the TESTFLOW-76 flow; node install and repair do not read or change account
credentials.

Tests: `tests/v4/test_node_operations.py` and `tests/v4/test_console.py`.
