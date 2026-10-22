# Safe containers for project checks

Projects can opt into a Docker-compatible runtime for checks that use `docker build`, `docker compose up`,
`docker compose run`, or `docker run` with the current worktree mounted. In Command Center, enable **Checks need
Docker containers** when adding a project. Existing projects can enable the runtime for a repository in
**Projects → Repositories**; choose **Inherit project setting**, **Require safe runtime**, or **Not required**.
The setting is saved as `containers: true` in the project or repository entry in `aiflow-control/projects/`.

## Node requirements

The node must provide rootless Podman, the `podman-compose` provider, and an XFS or ext4 storage filesystem
mounted with project quotas (`pquota` or `prjquota`). Podman must run as `aiflow-worker`; the runtime reports
`safe-container-runtime` as missing unless rootless mode, Compose, project quotas, and sufficient filesystem
capacity are available. There is no automatic fallback to a host Docker daemon. A node without the safe runtime
reports the missing tool, and work for a project that requires it stays out of that node's candidates.

Reserve at least 24 GiB on the container storage filesystem. AI Flow caps the rootless image and build cache at
12 GiB, each writable container layer at 1 GiB, concurrent run containers at eight, and preserves 4 GiB of free
space before starting container work. Build operations use a 2 GiB storage limit. These limits are enforced by
the broker and Podman's quota-backed storage options. The node agent removes unused images older than seven days
daily; images stay cached on that machine between runs.

## Run isolation and command policy

AI Flow starts a broker for each enabled dev, verify, or version-release run. The AI sees a run-local `docker`
shim and broker socket, not Podman's storage or an engine socket. The broker runs Podman as the unprivileged
worker user, assigns a unique Compose project to the run, and removes that run's containers, networks, and
volumes when it finishes. Images remain in the node's shared cache.

The policy permits builds whose context and Dockerfile resolve inside the worktree, Compose `up` and `run`, and
direct `docker run` with exactly one bind mount of that worktree. Compose files, build contexts, environment
files, configs, secrets, and bind mounts are resolved on the node and must stay inside the worktree, including
after symlink resolution. External volumes and secrets are rejected. Compose cannot enable privileged mode,
host namespaces, extra capabilities, host devices, or inherited mounts. Other Docker operations, direct Podman
commands, host-root mounts, `/etc/aiflow/secrets`, and paths in another worktree are denied.

The storage and mount checks run again in the node broker, after the control-plane policy hook. That keeps the
runtime safe for project check scripts as well as direct commands from an AI tool.
