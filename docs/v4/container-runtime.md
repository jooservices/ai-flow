# Safe containers for project checks

Projects can opt into a Docker-compatible runtime for checks that use `docker build`, `docker compose up`,
`docker compose run`, or `docker run` with the current worktree mounted. In Command Center, enable **Checks need
Docker containers** when adding a project. Existing projects can enable the runtime for a repository in
**Projects → Repositories**; choose **Inherit project setting**, **Require safe runtime**, or **Not required**.
The setting is saved as `containers: true` in the project or repository entry in `aiflow-control/projects/`.

## Node requirements

The node must provide rootless Podman, the `podman-compose` provider, and an XFS or ext4 storage filesystem
mounted with project quotas (`pquota` or `prjquota`). Podman must belong to a dedicated unprivileged account,
`aiflow-containers`, with a private (mode 0700) home and storage, subuid/subgid ranges, and a Podman binary that
`aiflow-worker` cannot execute (for example `dpkg-statoverride --update --add root aiflow-containers 0750
/usr/bin/podman`). The AI run and project checks stay `aiflow-worker`, so even calling the real Podman by absolute
path fails at the operating-system level. The runtime reports `safe-container-runtime` as missing unless that
separation, rootless mode, Compose, project quotas, and sufficient filesystem capacity are all verified.
Compose volumes may not set a custom `name`; they are always named per run. An opt-in integration test
(`AIFLOW4_TEST_PODMAN=1`) builds and runs a real container where the runtime exists. There is no automatic fallback to a host Docker daemon. A node without the safe runtime
reports the missing tool, and work for a project that requires it stays out of that node's candidates.

Reserve at least 24 GiB on the container storage filesystem. AI Flow caps the rootless image and build cache at
12 GiB, each writable container layer at 1 GiB, concurrent run containers at eight, and preserves 4 GiB of free
space before starting container work. Build operations use a 2 GiB storage limit. Writable worktree mounts have
a separate 4 GiB allocated-storage ceiling per run: the broker refuses a command if the checkout is already at
the limit, polls while commands or Compose services are active, and stops them when usage reaches the ceiling
or the filesystem falls to the 4 GiB reserve. When container storage and worktrees share a filesystem, reserve
at least 28 GiB total; on separate filesystems, reserve 24 GiB for container storage and 8 GiB for worktrees.
These limits are enforced by the broker and Podman's quota-backed storage options. The node agent removes unused
images older than seven days daily; images stay cached on that machine between runs.

## Run isolation and command policy

AI Flow starts a broker for each enabled dev, verify, or version-release run. The AI sees a run-local `docker`
shim and broker socket, not Podman's storage or an engine socket. The broker starts from the installed AI Flow
module with an isolated Python import path and a private working directory, then runs Podman as the separate
unprivileged `aiflow-containers` account. The AI process and project checks remain `aiflow-worker`. The broker
assigns a unique Compose project to the run, and removes that run's containers, networks, and volumes when it
finishes. Images remain in the node's shared cache.

The policy permits builds whose context and Dockerfile resolve inside the worktree, Compose `up` and `run`, and
direct `docker run` with exactly one bind mount of that worktree. Compose files, build contexts, environment
files, configs, secrets, and bind mounts are resolved on the node and must stay inside the worktree, including
after symlink resolution. External volumes and secrets are rejected. Compose cannot enable privileged mode,
host namespaces, extra capabilities, host devices, or inherited mounts. Other Docker operations, direct Podman
commands, host-root mounts, `/etc/aiflow/secrets`, and paths in another worktree are denied.

The storage and mount checks run again in the node broker, after the control-plane policy hook. That keeps the
runtime safe for project check scripts as well as direct commands from an AI tool.
