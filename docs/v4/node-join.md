# Join a node from Command Center

Command Center issues a one-use enrollment token for a node name, node role, and project. It expires five
minutes after creation. The control plane stores only its SHA-256 hash; redemption atomically consumes it and
creates the node credential. A copied token cannot be used for a second join, and an expired token is rejected.

The owner enters the configured `AIFLOW4_JOIN_TOKEN` as the provisioning key in the **Join a node** panel. This
is the existing control-plane secret, used only to authorize token creation. Command Center then shows the
copyable command:

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

After its first heartbeat, the machine appears in the **Machines and AI** screen. Open the machine there to add
an AI account by choosing its provider, name, roles (jobs), and worker count (sessions). The node creates the
account's private home and starts the provider sign-in handoff. Complete sign-in from the screen using the
provider link and code; when the node confirms the account is ready, it can take work for its assigned roles.
Provider credentials stay in the account's home on the machine and are not sent to the control plane.

Manage account assignments and sign-in from **Machines and AI**. Do not add assignments to `accounts.yaml` or
sign in through a shell; after the one-time import, the control database and screen own these runtime facts.
See [Machines and AI management](machines-and-ai.md) for account changes and machine operations.

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
