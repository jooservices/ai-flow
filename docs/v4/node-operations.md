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
with versions from `versions.lock`. Packages, Docker and PHP are delegated to the shared
[`jooservices/server-installer`](https://github.com/jooservices/server-installer) module. The node runtime
adapter expects its Python entry points `install_packages(packages=[...])`, `install_docker()`, and
`install_php(version=...)`; missing module/API errors fail the operation without falling back to shell package
managers. Other supporting tools use `install_tool(name=..., version=...)` from the same module.

`GET /v1/nodes/{name}/operations/next` is node-token protected. The agent claims one queued operation and posts
its verified result to `/result`. The Command Center log is `GET /v1/nodes/{name}/operations`, available only
to owner and lead sessions.

## Nodes without an agent

The owner can create a one-line `aiflow4 node join --join-token-stdin` command from the Command Center. Its
random join capability expires after 10 minutes, is accepted once for the requested node name and is stored in
PostgreSQL only as a SHA-256 hash. The command also enables the installed node-agent systemd unit. The owner
SSH password is neither requested nor stored. VM creation remains the separate TESTFLOW-55 flow.

AI account authorization remains the TESTFLOW-76 flow; node install and repair do not read or change account
credentials.

Tests: `tests/v4/test_node_operations.py` and `tests/v4/test_console.py`.
