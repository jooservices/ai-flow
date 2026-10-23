# Playwright toolchain

Setup leaves a machine where browser tests work without anyone typing commands. This is a developer and CI
toolchain step on the machines (node agent), so it has no end-user UI. See also
[Playwright browser](playwright-browser.md) for how projects select it.

## Toolchain step (`playwright`, `_install_playwright`)

- Runs `npm install --global playwright@<pin>` (pin from `versions.lock`).
- The Node under `/usr/local/lib/aiflow4/nodejs` keeps its global bin folder inside its own prefix. The step
  asks npm for the prefix (`npm prefix --global`) and links `<prefix>/bin/playwright` to
  `/usr/local/bin/playwright`, so `playwright` is on the PATH of the node agent and of AI runs.
- Works when the node agent has no `HOME` or npm cache variable: the step falls back to
  `/usr/local/lib/aiflow4/home` and `/usr/local/lib/aiflow4/npm-cache`.

## Browser step (`playwright-browser`, `_install_playwright_browser`)

- Runs `playwright install --with-deps chromium` (Chromium and its operating-system libraries, such as
  libnspr4) with `PLAYWRIGHT_BROWSERS_PATH=/usr/local/lib/aiflow4/playwright-browsers`, then makes that shared
  folder readable by the AI user (`aiflow-worker`).
- Runs get `PLAYWRIGHT_BROWSERS_PATH` through the project environment (`project_env`); a project's
  `/etc/aiflow/projects/<KEY>.env` may override it. The node agent's browser probe uses the same folder.
- Launch check: Chromium is started headless once as the AI user and its version is logged. A failure says what
  is missing in plain words, for example `Chromium cannot start: the system library libnspr4.so is missing`.

## Errors

Installer failures carry the installer's own last message (bounded to 400 characters) and are logged, not only
`exit N`.
