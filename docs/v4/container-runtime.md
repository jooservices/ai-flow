# Safe containers for project checks

**Console screens:** `projects`

Projects can opt into a Docker-compatible runtime for checks that use `docker build`, `docker compose up`,
`docker compose run`, or `docker run` with the current worktree mounted. In Command Center, enable **Checks need
Docker containers** when adding a project. Existing projects can enable the runtime for a repository in
**Projects → Repositories**; choose **Inherit project setting**, **Require safe runtime**, or **Not required**.
The setting is saved as `containers: true` in the project or repository entry in `aiflow-control/projects/`.

Compose runs never use `--project-directory` (`podman-compose` does not support it) and never load an implicit
`.env`: the broker reads the given `--env-file` files, or the project's default `.env`, through the pinned worktree
descriptor and passes the snapshot as the only `--env-file`. A symbolic-link `.env` is refused.

## Node requirements

The node must provide rootless Podman, the `podman-compose` provider, `xfs_quota` from xfsprogs, `setfacl` from
acl, and XFS filesystems mounted with project quotas (`pquota` or `prjquota`) for Podman storage, the engine
account's home (which holds per-run runtime directories), and `/srv/aiflow/work`. Podman must belong to a dedicated
unprivileged account,
`aiflow-containers`, with a private (mode 0700) home and storage, subuid/subgid ranges, and a Podman binary that
`aiflow-worker` cannot execute (for example `dpkg-statoverride --update --add root aiflow-containers 0750
/usr/bin/podman`). The AI run and project checks stay `aiflow-worker`, so even calling the real Podman by absolute
path fails at the operating-system level. The runtime reports `safe-container-runtime` as missing unless that
separation, rootless mode, Compose, the quota-enabled filesystems, successful hard-quota setup, `setfacl`,
worktree-filesystem ACL support, runtime-directory hard-quota support, and sufficient filesystem capacity are all
verified. The node preflight places a hard 12 GiB project quota on the shared Podman graphroot and probes that hard
per-worktree and per-run runtime-directory quotas can be assigned. Each container-enabled run gets a unique XFS
project ID and a hard 4 GiB quota on its checkout, plus a separate 1 GiB hard quota on its private runtime directory.
Compose volumes may not set a custom `name`; they are always named per run. An opt-in integration test
(`AIFLOW4_TEST_PODMAN=1`) builds and runs a real container where the runtime exists. There is no automatic fallback to a host Docker daemon. A node without the safe runtime
reports the missing tool, and work for a project that requires it stays out of that node's candidates.
Compose named volumes, downloaded images, and writable container layers all live in the rootless default store;
the graphroot's 12 GiB XFS project quota is a kernel-enforced hard cap on their combined growth. Podman's per-volume
`size=` option needs root, so AI Flow does not set it. Anonymous Compose volumes are rejected, with at most eight
named volumes per project.

Reserve at least 25 GiB on the container storage filesystem. The hard graphroot quota caps shared images, layers,
and named volumes at 12 GiB. Each writable container layer has an additional 1 GiB Podman limit, build operations
use a 2 GiB storage limit, and at most eight run containers may be active. The broker also preserves 4 GiB of free
filesystem space before starting container work. Each worktree has an independent hard 4 GiB project quota, so a
container write reaches the kernel limit even between broker checks. The broker's storage checks and watchdog
remain as early refusal and cleanup safeguards. When container storage and worktrees share a filesystem, reserve
at least 29 GiB total; on separate filesystems, reserve 25 GiB for container storage and 8 GiB for worktrees.
The node agent removes unused images older than seven days daily; images stay cached on that machine between runs.
After `compose up -d` returns, the broker continues checking the shared container store and worktree until the run
ends; it stops and removes the run's containers if either storage ceiling or the disk reserve is reached.

## Run isolation and command policy

AI Flow starts a broker for each enabled dev, verify, or version-release run. Compose snapshots and overrides are
removed after each command finishes, while the runtime directory's XFS quota bounds temporary files even if a
command creates them rapidly. The AI sees a run-local `docker`
shim and broker socket, not Podman's storage or an engine socket. The broker starts from the installed AI Flow
module with an isolated Python import path and a private working directory, then runs Podman as the separate
unprivileged `aiflow-containers` account. The AI process and project checks remain `aiflow-worker`. The broker
assigns a unique Compose project to the run, and removes that run's containers, networks, and volumes when it
finishes. Images remain in the node's shared cache.
At startup, the broker verifies the checkout's device and inode against the identity captured by the supervisor
and holds the worktree directory open until shutdown. A replaced checkout path fails closed. Each descendant passed
to Podman is opened one component at a time from the pinned directory with symlink following disabled; Podman gets
the stable proc-fd path, not a re-resolved mutable pathname. Compose files are read through descriptors opened
from that pinned directory with symlink following disabled, then their validated inputs are snapshotted in broker
storage.

The policy permits builds whose context and Dockerfile resolve inside the worktree, Compose `up` and `run`, and
direct `docker run` with exactly one bind mount of that worktree. Compose files, build contexts, environment
files, configs, secrets, and bind mounts are resolved on the node and must stay inside the worktree, including
after symlink resolution. External volumes and secrets are rejected. Compose cannot enable privileged mode,
host namespaces, extra capabilities, host devices, or inherited mounts. Other Docker operations, direct Podman
commands, host-root mounts, `/etc/aiflow/secrets`, and paths in another worktree are denied.

The storage and mount checks run again in the node broker, after the control-plane policy hook. That keeps the
runtime safe for project check scripts as well as direct commands from an AI tool.

## Run services started by the worker

The worker supervisor can start the services a run needs, such as the WordPress site (`wordpress-runs.md`), before
the AI process exists. It sends a `supervise` request, accepted only from the supervisor (uid 0) with the run token;
the command goes through the same policy, quota and cleanup path as AI commands. The first request also binds the
supervisor to the broker, so everything started is removed if the worker dies before the AI process starts.

## Control API

In **Projects**, the `containers` project or repository setting is checked during preflight, saved with the
control-repository project configuration, and used to hold a ticket until its node reports the safe runtime.
The relevant control API contracts are `POST /v1/projects/preflight`, `POST /v1/projects`,
`GET /v1/projects/{key}/repositories`, and `PUT /v1/projects/{key}/repositories`.

The node and worker admission path uses `GET /v1/version-releases/candidates`, `POST /v1/version-releases/claim`,
and `POST /v1/work/claim` to avoid offering container work to a node without the runtime. A run passes permitted
container commands through `POST /v1/policy/decide`; node operations remain managed through
`POST /v1/nodes/{name}/operations`.
