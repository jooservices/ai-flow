# Install the control plane with one command

`aiflow4 install control` sets up an AI Flow v4 control plane on a new Ubuntu 22.04 or 24.04 machine. The control
plane is control-api (two containers), control-loops, PostgreSQL with pgvector, Redis, Traefik, OpenObserve, Gatus
and Mailpit. You can run the same command again, give it a newer release to upgrade, and roll back. The OS steps
come from the owner's `jooservices/server-installer`. AI Flow does not contain its own copy of them.

## Install

On the new machine, as a user with sudo, run this command. Use a published release in place of `vX.Y.Z` and the
host name the console will be reached on in place of `aiflow.example.com`. The first Administrator is created in
the browser after install:

```sh
curl -fsSL https://raw.githubusercontent.com/jooservices/ai-flow/vX.Y.Z/deploy/v4/bin/aiflow4-control-install \
  | sudo bash -s -- --version vX.Y.Z --domain aiflow.example.com
```

`aiflow4-control-install` installs `curl`, `git`, cosign v3.1.4 (pinned digest) and uv. It downloads the release
tarball, `SHA256SUMS` and the Sigstore bundle, then checks the checksum and the signature identity
`.../release-v4.yml@refs/tags/vX.Y.Z`. Only after both checks pass does it install that release's `aiflow4`
command as `/usr/local/sbin/aiflow4`. Then it runs:

```sh
sudo aiflow4 install control --version vX.Y.Z --domain aiflow.example.com
```

On a host that already has `aiflow4`, run this second command directly. Every later run, upgrade or rollback uses
it. The steps run in this order, and each one prints `==> [n/12] step`:

| Step | What happens |
|---|---|
| preflight | root, Ubuntu 22.04 or 24.04, a `vX.Y.Z` version, a valid domain and valid `--stack`/`--si` values; one install at a time (`/run/aiflow4-install.lock`) |
| os | server-installer v1.1.0: `plan --modules M`, then `apply --modules M` (see below) |
| tools | AI Flow's own needs: `git`, `curl`, cosign, `aiflow4-deploy`, the `jooservices/ai-flow` checkout in `/srv/aiflow/control/repos/ai-flow` (for tag checks) |
| release | `aiflow4-deploy --stage-only vX.Y.Z`: release assets, SHA-256 and cosign checks of the tarball and of `ghcr.io/jooservices/ai-flow/control-api:vX.Y.Z`, compose bundle in `/srv/aiflow/build/vX.Y.Z` |
| secrets | generates any internal secrets that are missing (below), asks for an ownership capability on an interactive first install, and writes `/etc/aiflow/control.env` (domain and join URL; no secrets) |
| operator-audit | installs `auditd` and the interactive shell, configuration, and control-repository audit rules used by the Dashboard intervention ledger |
| infra | copies `deploy/v4/control/infra.yaml`, `gatus.yaml` and `deploy/v4/o2/lab.env` of the release to `/srv/aiflow/control/infra/`, then `docker compose up -d`; waits for PostgreSQL and Redis |
| migrate | for an upgrade, the pre-upgrade check first; then `aiflow4 migrate` from the release image |
| admin | Console login is on (`AIFLOW4_CONSOLE_LOGIN=1`). On first browser visit, `/setup` saves the public address and creates the first Administrator; no email or password file is required. A legacy `--admin-email` may still seed a user for controlled CI installs; see [console login](console-login.md) |
| deploy | `aiflow4-deploy vX.Y.Z`: control-api a and b one at a time, control-loops, `CURRENT` |
| health | every component (below); a failed component is named |
| cli | points `aiflow4` at the installed release (`aiflow4-control-install --cli-only vX.Y.Z`) |

Exit codes: `0` healthy; `2` wrong arguments or host; `3` OS preparation failed; `4` the release could not be
downloaded or verified; `5` a health check failed (after an upgrade, the previous release was restored and is
healthy); `6` the rollback is unhealthy too; `8` another install is running; `9` the pre-upgrade check blocked the
upgrade and nothing changed.

On a machine with a normal network connection, Command Center (`https://<domain>/cc`) answers within 10 minutes. With no Administrator seeded by a legacy option, `/login` shows the setup wizard and the first browser visit creates the Administrator without writing a file.
Most of that time goes to downloading Docker and the images. Gatus runs on port 8081 (`http://<domain>:8081/`) and
OpenObserve on `/o2/`. Traefik serves plain HTTP on port 80; terminate TLS at the public proxy and restrict origin
ingress to that proxy. The installer sets the public URL to HTTPS, so session cookies carry `Secure`.

Data is kept in `/srv/aiflow/control/data/{postgres,redis,openobserve}`. PostgreSQL and Redis are reachable only
on the internal `aiflow4-control` network. The control host runs no node agent (`AIFLOW4_DEPLOY_NODE_AGENT=0` in
`/etc/aiflow/control.env`). To run workers on it too, join it from Command Center like any other node.

### Health check

| Component | Check |
|---|---|
| postgres, redis | container health `healthy` (`pg_isready`, `redis-cli ping`) |
| traefik | `http://127.0.0.1/ping` |
| openobserve | `http://127.0.0.1/o2/healthz` through Traefik |
| gatus | `http://127.0.0.1:8081/health` |
| control-api-a, control-api-b | container health `healthy` and the image of the requested release |
| control-loops | running, with the image of the requested release |
| command-center | `/healthz` and `/cc` through Traefik |

The checks retry for up to 300 seconds (`--health-timeout`). A failure ends with, for example,
`health check failed: openobserve: http://127.0.0.1/o2/healthz does not answer`.

## Running it again

Running the same command a second time changes nothing. Existing secrets are kept, never regenerated. The release
is not downloaded again. `docker compose up -d` leaves running containers alone because their configuration has
not changed. Migrations find nothing new to apply. The data directories are not touched. The command ends with
`control plane already on vX.Y.Z` and the healthy message. A rerun with a different `--domain` updates only the
URLs, so control-api is recreated with the new join URL.

## Secrets and tokens

`install control` creates these internal secrets once. They live in `/etc/aiflow/secrets/control` (directory
`0700`, every file `0600`, owned by root):

| File | Values | Used by |
|---|---|---|
| `postgres.env` | `POSTGRES_PASSWORD` (and the user and database names) | PostgreSQL |
| `openobserve.env` | OpenObserve root user and password | OpenObserve |
| `aiflow4.env` | database and Redis URLs, `AIFLOW4_JOIN_TOKEN` (provisioning key), `AIFLOW4_OWNER_TOKEN`, `AIFLOW4_SETUP_OWNERSHIP_CAPABILITY`, `AIFLOW4_REVIEWER_TOKEN`, `AIFLOW4_CONSOLE_SESSION_SECRET`, `O2_BASIC` | control-api, control-loops |

On an interactive first install, the installer asks twice for a 32-character-or-longer ownership capability with
hidden input. Use that value in the browser's first-run setup screen. The installer stores it as
`AIFLOW4_SETUP_OWNERSHIP_CAPABILITY` in the same root-only `aiflow4.env` file, and the browser setup consumes it
atomically with first-Administrator creation. It is not returned by any public setup endpoint. A noninteractive
install generates a random capability; provide it to the first owner through the installation's secure credential
delivery channel before they open setup.

They are never printed. Command output that the installer captures has every secret value replaced by `***`.
They are not in the release tarball or the image: both are built from the repository, which holds no secrets
(the tests check this). The compose files only name the env files.

Values that only the owner has go in through a separate command with hidden input:

```sh
sudo aiflow4 secret set GH_TOKEN          # prompts twice, nothing is shown
sudo aiflow4 secret list                  # names only: set / not set
```

| Name | When |
|---|---|
| `GH_TOKEN` | the release or GHCR package is private: used by `gh`, `git` and `docker login ghcr.io` (password on stdin) |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | the API-token fallback; normally connect Jira on the Integrations screen ([Connect Jira](jira-connect.md)) |

The value is never an argument, so it is not in the shell history or the process list. To pipe it from a secret
manager instead, use `--stdin`, for example `secret-tool lookup aiflow gh | sudo aiflow4 secret set GH_TOKEN --stdin`.
A value for control-api (`JIRA_*`) restarts control-api a, then b, then control-loops so it takes effect.

## Upgrade

```sh
sudo aiflow4 install control --version vX.Y+1.Z --domain aiflow.example.com
```

The steps are the same. Before any migration, the pre-upgrade check of the new image runs: migrations only add
files, and every online node must stay within the version skew policy ([Fleet upgrade](fleet-upgrade.md)). Then
the new migrations are applied, `aiflow4-deploy` switches the containers (without repeating the check), and the
health check runs. Nodes follow `CURRENT` through the usual canary and waves.

## Rollback

If the health check fails after an upgrade, the command rolls back on its own. It runs
`aiflow4-deploy <previous>` with `AIFLOW4_SKIP_UPGRADE_CHECK=1` (the older release lacks the newer migrations,
and they stay because migrations are forward only), checks the health again, and exits with `5` and a message
naming the failed component and the restored release. If the rollback is unhealthy too, it exits with `6`.

The owner can roll back at any time:

```sh
sudo aiflow4 install control --rollback
```

This goes back to the newest staged release below `CURRENT` in `/srv/aiflow/control/releases/` (the last ten are
kept), then runs the health check. Running it again goes back one more release. `--version <older>` also works.
It skips the check and the migration. A rollback changes only the AI Flow containers and `CURRENT`. PostgreSQL,
Redis, OpenObserve and their data stay as they are.

## OS preparation: pinned server-installer

The OS layer is `jooservices/server-installer` at the tag **v1.1.0**
(`https://github.com/jooservices/server-installer/archive/refs/tags/v1.1.0.tar.gz`). It is installed in
`/opt/server-installer/v1.1.0/` and run as an external CLI. This is the same pin as the node bootstrap and node
operations (`SERVER_INSTALLER_VERSION` in `aiflow/v4/node/operations.py`).

| Modules | When |
|---|---|
| `packages,timesync,swap,ulimits,docker,docker_group` | always (`docker` brings the compose plugin; `docker_group` does nothing for root) |
| `php` | `--stack php-X.Y`, or `--si SI_PHP_VERSION=X.Y` |
| `otel_collector` | `--otel-collector` |
| `tailscale` or `wireguard` | `--vpn tailscale` or `--vpn wireguard` |

The ticket gives `server-installer apply --modules ... --preflight` as an example. v1.1.0 has no `--preflight`
option: its argument parser rejects unknown flags. The CLI offers `doctor`, `plan`, `apply` and `verify`. Each
module's preflight metadata (`metadata/modules/<id>.json`: OS family, architecture, systemd, Docker) is checked
when the module loads. So the installer runs `plan --modules M`, which shows the preflight result and what would
change, and then `apply --modules M`, which skips modules that are already satisfied. That second step is why a
rerun changes nothing. When a server-installer release with `--preflight` is pinned, change the pin and these
two calls together.

### Project toolchains: `SI_*`

PHP and other OS-level toolchains go to server-installer only as `SI_*` variables. Inherited `SI_*` variables are
dropped, so only the values given on the command line reach it:

```sh
sudo aiflow4 install control --version vX.Y.Z --domain aiflow.example.com --stack php-8.3     # TESTWP
sudo aiflow4 install control ... --si SI_PHP_VERSION=8.3 --si SI_PHP_MODE=fpm
```

`--stack php-8.3` sets `SI_PHP_VERSION=8.3` and `SI_PHP_MODE=cli` and adds the `php` module. That is what TESTWP
(stack `php-8.3`) needs. Userland tools (uv, Node, the AI CLIs, Composer) belong to AI Flow and are installed on
nodes ([Project toolchains](project-toolchains.md)).

## Proposed server-installer profiles

These two profiles are proposals for `jooservices/server-installer`. Its owner or lead creates them in that
repository; this ticket does not. Once a tag with them is pinned, the module lists above can be replaced with
`--profile aiflow-control` and `--profile aiflow-node`.

- `aiflow-control`: modules `packages,timesync,swap,ulimits,docker,docker_group`, with optional `otel_collector`
  and `tailscale` or `wireguard`. No env defaults. The containers, secrets, migrations, health checks and release
  handling stay in AI Flow.
- `aiflow-node`: modules `packages,timesync,swap,ulimits,docker,docker_group`, with optional `tailscale` or
  `wireguard`, and project settings through `SI_*` (`SI_PHP_VERSION`, `SI_PHP_MODE`). The `aiflow-worker` user,
  node agent, workers, AI CLIs, join token and signed release updates stay in AI Flow.

## Files

- `aiflow/v4/install/control.py`, `aiflow/v4/install/secrets.py`: `aiflow4 install control` and `aiflow4 secret`.
- `deploy/v4/bin/aiflow4-control-install`: the first-install and CLI bootstrap.
- `deploy/v4/control/infra.yaml`, `deploy/v4/control/gatus.yaml`: the services, with pinned images.
- `deploy/v4/bin/aiflow4-deploy`: reads `/etc/aiflow/control.env`, falls back to the public release download when
  `gh` is not signed in, and skips the node agent when `AIFLOW4_DEPLOY_NODE_AGENT=0`.
- `deploy/v4/ci/control-install-smoke.sh`: the container acceptance run.

## Verification

Automated (`tests/v4/test_control_install.py`, with a fake host for server-installer, Docker and HTTP): the order
of the steps, the internal secrets and their modes, the named failing component, a second run that changes
nothing, the upgrade order (check, migrate, deploy), automatic and requested rollback, the blocked upgrade,
secrets kept out of output, argv, package and image, hidden secret entry, the pinned server-installer tag with
`plan`/`apply`, and `SI_*` values.

Ubuntu 24.04 acceptance runs in a clean systemd container. `deploy/v4/ci/control-install-smoke.sh <previous>
<target>` builds `deploy/v4/ci/systemd.Dockerfile` and runs five checks:

1. A clean install, which must finish within 10 minutes.
2. The identical second run, which must not change secret hashes, container ids, a PostgreSQL row or a Redis key.
3. An upgrade whose health check is made to fail (`AIFLOW4_INSTALL_SIMULATE_UNHEALTHY=control-api-a`, used only by
   CI), which must roll back to `<previous>` and exit 5.
4. A real upgrade.
5. `--rollback`.

Both versions must be published releases that contain this installer, so the first run is possible once two such
releases exist. For Ubuntu 22.04, the non-systemd parts were checked: argument parsing, the module list and
`SI_*` values, the config and secret rendering, and `bash -n` of the scripts. These are the 22.04 cases in the test
file.

Owner-verified (a real machine is needed; this does not block Done): the 10-minute install on real 22.04 and
24.04 hosts with their network and disks; a private GHCR package pulled with `GH_TOKEN`; `--vpn` and
`--otel-collector`; and TLS in front of Traefik. Record the date, the host and the result here when they are done.
