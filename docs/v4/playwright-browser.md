# Playwright browser on the machines

Developer AIs that work on console screens can open them in a real headless Chromium. A project asks for it with
the `playwright` stack in `aiflow-control/projects/<KEY>.yaml`:

```yaml
stack: [node-24, playwright]
```

Pin the versions in `aiflow-control/versions.lock`:

```yaml
tools:
  node: "24.x"
  playwright: "1.56"
```

The same pinned Playwright package installs Chromium, Firefox and WebKit. A `wordpress` project stack includes
these browser requirements and Podman so its run can use the isolated site described in
[WordPress runs](wordpress-runs.md).

## Installation

Every machine serving a project with the `playwright` stack installs, in this order, at the pinned versions:

1. Node.js (official checksummed distribution).
2. The Playwright package from npm.
3. The Chromium, Firefox and WebKit builds selected by that Playwright version, with their system libraries. The
   node agent installs each browser with `playwright install --with-deps <browser>` as root.

A stack without a Playwright pin is not installed and its project stays gated until a pin exists.

## Reporting

The node heartbeat reports `node`, `playwright`, `chromium`, `firefox` and `webkit`. Each browser is reported with
its name only when the build expected by the installed Playwright exists and launches headless. The shared browser
directory is `/usr/local/lib/aiflow4/playwright-browser`, passed to runs as `PLAYWRIGHT_BROWSERS_PATH`; it is a
folder, not a command.

## Missing system libraries

If a browser is absent, the missing-tool report names it (for example `firefox`). If it is installed but cannot
start, for example because `libnss3.so` is missing, that browser is reported as broken with the same name. It
counts as a gap in the machine's toolchain: the node drawer shows it, the watchdog opens the warning incident
`node <name> misses tools for <PROJECT>`, and the node installer repairs it with `--with-deps` on the next
preparation. The incident closes when a later heartbeat reports a working browser. It is never counted as a
ticket failure.
