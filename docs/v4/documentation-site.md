# Product documentation site

The product documentation lives in `docs/guide/`, is built into a static site by `aiflow/v4/core/docsite.py`
(standard library only, no new dependency), ships in every release and is shown through the console Help links.

## What is published

| Part | Source |
|---|---|
| User guide per console role (`super_admin`, `owner`, `lead`) | `docs/guide/user/<role>.md` |
| One page per console screen | `docs/guide/screens/<view>.md` |
| Administrator guide: install, upgrade, backup, security | `docs/guide/admin/` |
| Runbooks, one page per incident kind | `docs/guide/runbooks/<kind>.md` |
| API reference | generated from the FastAPI routes in `control/api.py`; never written by hand |

Every page states who it is for and what it assumes in a line that starts with `**Assumes:**` (a test enforces it).
Keep pages in plain language, and never put a secret, a token or the content of `/etc/aiflow` in them.

## Runbooks follow the code

A runbook page exists for every kind that the code in `aiflow/v4/control/*.py` passes to `open_incident` (a literal
or a module constant such as `INCIDENT_KIND`). The test `tests/v4/test_documentation.py` finds the kinds by
reading the code and fails when a kind has no page, a page has no kind, or the runbook index misses one. When you
raise a new kind, add `docs/guide/runbooks/<kind>.md` (symptom, cause, fix) and a row in `runbooks/index.md`. The
master AI's `incident_open` action only accepts these kinds (control answers 400 for any other), so every
incident has a page.

The CI check (`docsite check`) ties documentation to the change. A change in `aiflow/v4/control/ui/` needs a changed
page under `docs/guide/screens/` (the page of each screen whose section changed, when the change is inside a screen
section). A change in any control module that registers `/v1/...` routes (found from the app's routes, so
`api.py`, `login.py` and `jira_connect.py` today) needs a changed page in `docs/guide/` or `docs/v4/` that names one
of that module's routes. An unrelated documentation edit does not pass.

## Build

```sh
uv run --frozen python -m aiflow.v4.core.docsite build --out build/docs-site --version vX.Y.Z
```

The build fails on a broken link between pages. In the release workflow `deploy/v4/ci/add-docs-site.sh VERSION
TARBALL` adds `docs-site/` to the tarball before it is checksummed and signed. The control image builds the same
site to `/app/docs-site`.

## In the console (UI path)

- The **Help** link in the top bar opens `/help/screens/<view>.html`, the page of the screen you are on. Control
  serves the site at `/help/` from `docs-site/` of the release (or `$AIFLOW4_DOCS_SITE`; from a source checkout it
  builds a copy on first use). The console login protects it like any other path.
- Each screen shows a short first-use hint with a link to its page; **Got it** hides it for that screen in that browser.
- Adding a screen means adding a `view-<name>` section, an entry in `HINTS` in `console.html` and
  `docs/guide/screens/<name>.md`; the tests fail otherwise.

## CI check

The `Documentation` job in `.github/workflows/ci.yml` builds the site and, on pull requests, runs

```sh
uv run --frozen python -m aiflow.v4.core.docsite check --base origin/<target branch>
```

It fails when the pull request changes `aiflow/v4/control/ui/` or `aiflow/v4/control/api.py` and changes no `.md`
file under `docs/guide/` or `docs/v4/`. It passes as soon as one such documentation file changes in the same
pull request; reviewers judge whether the change is the right one.
