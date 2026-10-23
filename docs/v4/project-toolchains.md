# Project toolchains on every node

Each project declares the stack its nodes must prepare in
`aiflow-control/projects/<KEY>.yaml`. Keep a single stack as a string for older projects, or list multiple
stacks when a project uses several runtimes:

```yaml
key: WEB
repo: jooservices/web
stack: [php-8.3, node-24, playwright]
```

Supported stacks are mapped in `aiflow/v4/core/toolchains.py`. `php-8.3` requires PHP, Composer and WP-CLI;
`node-24` requires Node.js 24; `playwright` requires Node.js, the Playwright package, and Chromium, Firefox and
WebKit. `wordpress` requires Podman and the full Playwright toolchain, and starts its per-run site in Podman. The
project toolchain also includes the worker's base Git and uv tools.

Pin stack versions in the control repo's `versions.lock` under `tools`:

```yaml
tools:
  php: "8.3"
  composer: "2.8"
  wp: "2.12"
  node: "24.x"
  playwright: "1.56"
  podman: "4.9"
```

Pins may be a full version or a numeric release line such as `8.3`, `2.8` or `24.x`. Preflight checks the
reported version against the pin. Chromium, Firefox and WebKit use the Playwright pin because their revisions are
selected by that Playwright release. A mismatched or absent pin makes that project's stack unhealthy.

On its first heartbeat, and again when the AI Flow release, project stack declaration or `versions.lock` changes,
the node agent prepares the configured project stacks on the host and reports a fresh probe. PHP uses the pinned
jooservices/server-installer PHP module with `SI_PHP_VERSION` set to the locked major.minor. Composer uses the
official installer after checking its published SHA-384 signature. WP-CLI is
installed from its official release Phar. Node.js uses its official checksummed distribution; Playwright is
installed from npm and installs all three browsers with the Playwright CLI. These are host tools; the project stack
does not use a toolchain Docker image. WordPress tests use the separate isolated site runtime documented in
[WordPress runs](wordpress-runs.md).

The control API checks the latest node report before offering work. If preflight still fails after an install
attempt, the node receives no more tickets for that project, while eligible work for other projects on that node
remains available. The watchdog opens a warning `node_tools` incident for the affected node and project. The
agent retries when its AI Flow release, project stack declaration or `versions.lock` changes. A later heartbeat
that passes preflight clears the incident and makes the project eligible again.

The Command Center's node repair operation uses the same pins and installer whitelist for a manual full-state
reconciliation.
