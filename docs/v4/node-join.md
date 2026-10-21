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

Enrollment does not carry an AI provider credential, API key, OAuth token, or AI account home. AI Flow returns
only the account IDs already assigned to that node in `aiflow-control/accounts.yaml`, then creates their
separate directories under `/srv/aiflow/accounts/` owned by `aiflow-worker`.

Map an account to the node name in `aiflow-control/accounts.yaml` before installing if the node should start
work immediately. After installation, the owner completes that provider's sign-in as `aiflow-worker`. For a
Codex account, for example:

```sh
sudo -u aiflow-worker env HOME=/srv/aiflow/accounts/codex-01 codex login --device-auth
```

Other installed CLIs use their own interactive sign-in command, such as `claude auth login` or
`opencode auth login`, with `HOME` set to the matching account directory. Authentication files stay in that
directory and are never sent to the control plane. Once an assigned account is signed in, its worker probes
the CLI, the node reports online with its required tools, and the worker can claim ready tickets in the
enrolled project.

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
