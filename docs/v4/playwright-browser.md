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

## Installation

Every machine serving a project with the `playwright` stack installs, in this order, at the pinned versions:

1. Node.js (official checksummed distribution).
2. The Playwright package from npm.
3. The Chromium build selected by that Playwright version, with its system libraries
   (`playwright install --with-deps chromium`; the node agent runs as root).

A stack without a Playwright pin is not installed and its project stays gated until a pin exists.

## Reporting

The node heartbeat reports `node`, `playwright` and `playwright-browser`. The browser line reads
`Playwright Chromium <version>` only when the build expected by the installed Playwright exists **and** starts:
the agent launches it once headless (`--headless --no-sandbox`, no extra settings) and checks it exits cleanly.

## Missing system libraries

If the browser is installed but cannot start, for example `error while loading shared libraries: libnss3.so`,
the agent reports `broken: missing system library libnss3.so` for `playwright-browser`. That counts as a gap
in the machine's toolchain: the node drawer shows it, the watchdog opens the warning incident
`node <name> misses tools for <PROJECT>`, the node installer repairs it with `--with-deps` on the next
preparation, and the incident closes when a later heartbeat reports a working browser. It is never counted
as a failure of a ticket.
