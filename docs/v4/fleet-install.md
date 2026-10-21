# Install an AI Flow v4 node fleet

The Ansible wrapper prepares a control host and joins node hosts to an existing AI Flow v4 control plane. It
uses the pinned `jooservices/server-installer` v1.1.0 CLI for OS modules with `--preflight`; the signed
AI Flow `/v1/install.sh` bootstrap owns the AI Flow user, agent, worker, AI CLIs, release verification and
join. The control role only prepares its OS. After that, deploy the signed control release with the existing
TESTFLOW-70 control deployment flow.

## Ansible inventory and run

Copy `deploy/v4/ansible/inventory.example.yml`, replace its example addresses and control URL, and keep the
ten node records in one `aiflow4_nodes` group. The sample sets each node's name, role, project, CPU quota,
memory maximum, worker slot count and `SI_PHP_VERSION=8.3` / `SI_PHP_MODE=cli` for TESTWP. CPU and memory
limits apply to a shared systemd slice containing the node-agent and its workers. Slots set `AIFLOW4_SLOTS`,
which the agent reports on heartbeat. PHP and other project-specific host modules pass only through `SI_*`
settings to server-installer.

Supply the control provisioning key to Ansible through the runner's secret environment, not the inventory,
command line, or user-data:

```sh
export AIFLOW4_PROVISION_KEY  # populate this from the approved secret manager
cd deploy/v4/ansible
ansible-playbook -i inventory.example.yml playbook.yml
```

The play uses ten forks for node hosts, so the ten hosts can prepare and join concurrently. The node role requests a short-lived one-use
token for each inventory entry, installs it in a root-only file under `/run`, and calls AI Flow's
`/v1/install.sh --token-file PATH`. The bootstrap deletes the token file as soon as it reads it. Ansible
suppresses tasks that handle the provisioning key or token. A matching existing node installation is detected
from its node name, release executable and token file, so reruns leave it joined and do not request another
token. If a machine has an AI Flow environment file for a different node name, the role stops before changing
it.

To target only nodes, add `--limit aiflow4_nodes`; to prepare only the control host, use
`--limit aiflow4_control`. The control role runs
`server-installer apply --modules packages,timesync,docker,docker_group,ulimits --preflight`; the node role
runs `server-installer apply --modules packages,timesync,docker,docker_group --preflight`, plus the PHP module
when `SI_PHP_VERSION` is configured. The role downloads the pinned CLI release and invokes its modules; it
does not implement package, time-sync, Docker, PHP, or limits installation itself.

## cloud-init samples

The provider examples are in:

- `deploy/v4/cloud-init/proxmox/user-data.yaml` for a Proxmox cloud-init snippet.
- `deploy/v4/cloud-init/virtualbox/user-data.yaml` for VirtualBox NoCloud seed data.
- `deploy/v4/cloud-init/cloud/user-data.yaml` for a generic cloud image.

Each example downloads AI Flow's join bootstrap and runs it with `--token-file`. Replace the sample control
URL and hostname before creating guests. Deliver the one-use token separately through the provider's protected
secret channel at `/run/secrets/aiflow4/join-token`; do not embed a token in reusable user-data. The API
provisioning key must remain on the trusted provisioning runner. The enrollment token is bound to the chosen
node name, role and project, expires after five minutes, and is consumed once.

## server-installer profile proposal

The `aiflow-control` and `aiflow-node` profiles are proposals for the
`jooservices/server-installer` repository. Profile implementation belongs to that repository; the AI Flow
fleet roles currently use its existing module interface.

- `aiflow-control`: compose `packages,timesync,docker,docker_group,ulimits`. Keep control secrets, app
  containers, database migration, health checks and release deployment in AI Flow's signed control release.
- `aiflow-node`: compose `packages,timesync,docker,docker_group,swap,ulimits`, with optional `tailscale`
  or `wireguard` and project `SI_*` settings such as `SI_PHP_VERSION`. Keep `aiflow-worker`, account
  homes, node agent, worker, AI CLIs, signed release updates and join-token handling in AI Flow.

Creating either profile is a change for the server-installer owner/lead, not this repository.

