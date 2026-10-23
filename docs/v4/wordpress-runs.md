# Isolated WordPress test sites

A project opts into a disposable site by declaring the `wordpress` stack. The stack prepares Podman, Node, the pinned
Playwright package, and Chromium, Firefox and WebKit on each eligible node:

```yaml
key: JOOWP
repo: jooservices/wordpress-plugin
stack: wordpress
```

Pin Podman, Node and Playwright in `aiflow-control/versions.lock` under `tools`. The three browser requirements use
the Playwright pin; if the repository has a local Playwright dependency, keep its package lock on that same release
because `npx` uses the project's local package. Set the WordPress core release for this project in the node-local
`/etc/aiflow/projects/<KEY>.env` file:

```dotenv
WP_CORE_VERSION=6.8.3
```

The same project environment file must be present on each node that serves this project. The admin account and
database password are generated for each run and are not written to project files or run notes.

## Site lifecycle

Before a dev or verify AI starts, the worker creates a uniquely named Podman network, database container, data volume,
and WordPress container. The site uses `wordpress:<WP_CORE_VERSION>-php8.3-apache` and `mariadb:11.4`. The web port
is dynamically published on loopback, so concurrent runs have separate URLs and do not need a host Docker daemon or
a shared integration site, and Podman does not depend on a host Docker daemon. Setup has an 85-second deadline; a site
that does not become ready inside that limit is removed and the run fails with the setup reason.

If the worktree contains a plugin header in a root or immediate child PHP file, that plugin is mounted read-only into
`wp-content/plugins` and activated. A theme with a `Theme Name` header in its `style.css` is mounted into
`wp-content/themes` and activated. A WordPress core worktree uses the default theme shipped with the pinned core.
The site is initialized with a per-run admin user, a published seeded post and a published seeded page.

The worker passes these values only to the AI process as environment variables:

| Variable | Value |
| --- | --- |
| `WP_BASE_URL` | This run's loopback site URL |
| `WP_ADMIN_USER` | This run's generated admin username |
| `WP_ADMIN_PASSWORD` | This run's generated admin password |

Dev and verify notes also show `npx playwright test`, which discovers the project's Playwright configuration, and
identify Chromium, Firefox and WebKit as installed. The browser folder is
`/usr/local/lib/aiflow4/playwright-browser`, passed as `PLAYWRIGHT_BROWSERS_PATH`; `playwright-browser` is not a
command. If a browser is absent or cannot launch, the report names Chromium, Firefox or WebKit individually.

The site stays available for the full AI process, including repeated test loops such as JOOWP-243's 20 KernelTest
runs. When the process exits, the worker removes both containers, the data volume and the network. Projects without
the `wordpress` stack do not call the site provisioner and incur no site startup cost.

## Browser reports from a run

When a run stops with `POST /v1/escalations/raise` because a browser cannot start, the control API names the missing
browser (`chromium`, `firefox` or `webkit`) as the requirement. It never reports `playwright-browser`, which is only
the browser folder.

## Lab acceptance proof

The two-machine lab proof for JOOWP-204, JOOWP-210 and JOOWP-243 is a post-merge canary check, per the owner
decision recorded on 2026-10-24. The canary should record each ticket's acceptance results and the two node names;
the developer run does not have access to deploy the change or start those runs.
