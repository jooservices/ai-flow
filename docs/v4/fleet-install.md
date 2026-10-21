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

Because the role already ran server-installer, it calls the bootstrap with `AIFLOW4_SERVER_INSTALLER_PREPARED=1`,
and the bootstrap then skips its own server-installer steps. If the joined project's stack is `php-X.Y`, the
bootstrap first checks that `php` is installed and reports version `X.Y`. It does this after inspecting the token
and before redeeming it. When the check fails, it stops with
`project KEY needs PHP X.Y, but php is …; run server-installer with SI_PHP_VERSION=X.Y`. The token stays
unused and the host is unchanged. Fix `aiflow4_node_si_environment` in the inventory and run the play again.

## cloud-init samples

The provider templates are in:

- `deploy/v4/cloud-init/proxmox/user-data.yaml` for a Proxmox cloud-init snippet.
- `deploy/v4/cloud-init/virtualbox/user-data.yaml` for VirtualBox NoCloud seed data.
- `deploy/v4/cloud-init/cloud/user-data.yaml` for a generic cloud image.

The join token works once and expires five minutes after it is minted. So each VM gets its own user-data, built
just before the VM boots. `deploy/v4/bin/aiflow4-cloud-init-user-data` reads `AIFLOW4_PROVISION_KEY` from
the runner's secret environment and mints a token bound to the node name, role and project. It then fills in
the template's placeholders and writes the result as a 0600 file. The command never prints the token; it
prints the expiry time. Cloud-init's `write_files` puts the token in `/run/aiflow4/join-token` (0600, tmpfs).
A single `runcmd` entry runs with `sh -e`. It stops at once if the token file is missing or empty, or if the
download fails. The bootstrap deletes the token file as soon as it reads it. Mint the token as the last step
before boot. If the VM does not reach `runcmd` within five minutes, the token expires unused: render the
user-data again and reboot.

```sh
export AIFLOW4_PROVISION_KEY  # populate this from the approved secret manager
R=deploy/v4/bin/aiflow4-cloud-init-user-data
```

- **Proxmox**: `$R --provider proxmox --name aiflow-node-01 --project TESTWP --control-url https://control
  --out /var/lib/vz/snippets/aiflow-node-01.yaml`, then
  `qm set VMID --cicustom user=local:snippets/aiflow-node-01.yaml && qm start VMID`. After the node shows up
  as joined, delete the snippet and run `qm set VMID --delete cicustom`.
- **VirtualBox (NoCloud)**: `$R --provider virtualbox ... --out seed/user-data`, write `seed/meta-data`
  (`instance-id` and `local-hostname`), build the seed with `cloud-localds seed.iso seed/user-data
  seed/meta-data`, attach it with `VBoxManage storageattach`, then `VBoxManage startvm`. Detach and delete
  `seed.iso` and `seed/` after the join.
- **Cloud**: `$R --provider cloud ... --out aiflow-node-01.user-data`, then pass it at creation, for example
  `--user-data file://aiflow-node-01.user-data`. Delete the local file afterwards. The provider's metadata
  service and `/var/lib/cloud/instance/user-data.txt` (root-only) keep a copy, but the token in it has
  already been used or has expired.

The API provisioning key stays on the trusted provisioning runner and never goes into user-data.

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

## Verification record

Checks for this change (TESTFLOW-72), run on the AI Flow developer runner on 2026-10-21:

- `ansible-playbook -i inventory.example.yml --syntax-check playbook.yml` (ansible-core via `uvx`): passed.
- `ansible-lint --offline playbook.yml roles`: passed, 0 failures and 0 warnings, `production` profile.
- `tests/v4/test_fleet_install.py`: runs both checks above when the tools are installed; the 10-node concurrent
  join and the rerun without duplicates; the bootstrap PHP check for a missing and a mismatched `php`; and
  the per-VM user-data renderer against the control API.

Still open: a run against lab VMs (Proxmox or VirtualBox guests joining a lab control plane) was **not** done
from this runner, because it has no hypervisor or container access. Record it here before rolling out to a
fleet, with the date, inventory size and result. Only run it in the lab, never on real machines outside it.

