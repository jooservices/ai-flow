# Playwright toolchain

Setup leaves a machine where browser tests work without anyone typing setup commands. This is a developer and CI
toolchain step on the machines (node agent), so it has no end-user UI. See [Playwright browser](playwright-browser.md)
for how projects select it.

## Toolchain step (`playwright`, `_install_playwright`)

- Runs `npm install --global playwright@<pin>` (pin from `versions.lock`).
- The Node under `/usr/local/lib/aiflow4/nodejs` keeps its global bin folder inside its own prefix. The step
  asks npm for the prefix (`npm prefix --global`) and links `<prefix>/bin/playwright` to
  `/usr/local/bin/playwright`, so `playwright` is on the PATH of the node agent and of AI runs.
- Works when the node agent has no `HOME` or npm cache variable: the step falls back to
  `/usr/local/lib/aiflow4/home` and `/usr/local/lib/aiflow4/npm-cache`.

## Browser folder and browser checks

- Installs Chromium, Firefox and WebKit separately with `playwright install --with-deps <browser>` and their
  operating-system libraries, then makes the shared folder readable by the AI user (`aiflow-worker`).
- Launches each browser headless as the AI user before the node reports it present. Failures identify the browser
  and, when available, the missing system library.
- `playwright-browser` is the folder `/usr/local/lib/aiflow4/playwright-browser`, never an executable.
- Runs get `PLAYWRIGHT_BROWSERS_PATH` through the project environment (`project_env`); a project's
  `/etc/aiflow/projects/<KEY>.env` may override it. The node agent's browser probe uses the same folder.
- Browser test notes tell the AI to run `npx playwright test`; Playwright discovers and uses the project's config.
  The run identifies Chromium, Firefox and WebKit as installed browsers.

## Errors

Installer failures carry the installer's own last message (bounded to 400 characters) and are logged, not only
`exit N`.
