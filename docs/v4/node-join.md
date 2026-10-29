# Join a node from Command Center

Command Center issues a one-use enrollment token for a node name, node role, and project. It expires five
minutes after creation. The control plane stores only its SHA-256 hash; redemption atomically consumes it and
creates the node credential. A copied token cannot be used for a second join, and an expired token is rejected.

An Administrator with machine management permission names the machine and chooses its role and project in **Machines
and AI**. Command Center creates a five-minute one-use enrollment grant and shows the copyable command. The browser
session authorizes token creation through `POST /v1/nodes/{name}/join-command`; the owner never has to retrieve or type the control plane's provisioning secret.
```sh
curl -fsSL https://<control>/v1/install.sh | sudo sh -s -- --token T
```

The bootstrap script first checks the token and reads its bound project stack. It downloads and runs the
`jooservices/server-installer` CLI pinned to **v1.1.0** with
`server-installer apply --modules packages,timesync,docker,docker_group --preflight`. For a PHP project it
also passes that project's version as `SI_PHP_VERSION` and `SI_PHP_MODE=cli` to the `php` module. It creates
the `aiflow-worker` system user and per-account homes, installs the shared uv-managed Python 3.12 under
`/opt/aiflow/python` (without replacing the operating system's `python3` command), installs the project tools
and AI CLIs, downloads the current signed AI Flow release, verifies its checksum and cosign signature, then
extracts and runs that release's `deploy/v4/bin/aiflow4-node-install` helper. The installed node agent then
follows the current signed release on later heartbeats. The equivalent CLI flow accepts a short-lived token file
or standard input with `aiflow4 node join --join-token-file /path/to/token` or
`aiflow4 node join --join-token-stdin`; the node name, role, and project come from the token grant.

## Accounts and sign-in

Enrollment does not carry an AI provider credential, API key, OAuth token, or AI account home. After the machine
joins, add its AI accounts in **Machines and AI**. AI Flow creates each private account home on the machine and
starts the provider sign-in handoff there; owners do not map accounts in `accounts.yaml` or run provider login
commands in a terminal.

For each pending account, **Machines and AI** shows the provider's sign-in link and code. Open the link in a
browser, enter the code when asked, then finish sign-in from the screen after the machine confirms the account is
ready. Provider credentials stay in that account's home on the machine and are never sent to the control plane.
The one-time machine join command above is the only setup action that uses a terminal. After the one-time
inventory import, the control database and **Machines and AI** own these runtime facts; see
[Database-backed inventory](inventory-in-database.md).

## server-installer profile proposal

The `aiflow-control` and `aiflow-node` profiles belong in the `jooservices/server-installer` repository and
should be implemented there by its owner or lead. This AI Flow change uses the pinned CLI and existing modules;
it does not create a second Ansible installer.

- `aiflow-control`: compose a baseline of `packages,timesync,docker,docker_group,ulimits`; keep control-plane
  secrets, application containers, migration, and health checks in AI Flow's signed control release.
- `aiflow-node`: compose `packages,timesync,docker,docker_group,swap,ulimits`; support optional `tailscale` or
  `wireguard`, and accept project-specific `SI_*` values such as `SI_PHP_VERSION` and `SI_PHP_MODE=cli`.
  Keep `aiflow-worker`, AI CLIs, account homes, node agent, release updates, and join-token handling in AI Flow.
- The optional Ansible fleet wrapper should invoke the version-pinned `server-installer` CLI on each host and
  pass profile/module arguments through. It should not implement module installation a second time.

The node is visible as online after its first heartbeat. Its required tools and project are shown in the node
drawer and missing required tools produce the existing `node_tools` warning until a heartbeat confirms them.
