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
| API reference | generated from the mounted FastAPI control app, including JSON routes mounted with the console; never written by hand |

Every page states who it is for and what it assumes in a line that starts with `**Assumes:**` (a test enforces it).
Keep pages in plain language, and never put a secret, a token or the content of `/etc/aiflow` in them.

## Runbooks follow the code

A runbook page exists for every kind that the code in `aiflow/v4/` passes to `open_incident` (a literal
or a module constant such as `INCIDENT_KIND`). The test `tests/v4/test_documentation.py` finds the kinds by
reading the code and fails when a kind has no page, a page has no kind, or the runbook index misses one. When you
raise a new kind, add `docs/guide/runbooks/<kind>.md` (symptom, cause, fix) and a row in `runbooks/index.md`. The
master AI's `incident_open` action only accepts these kinds (control answers 400 for any other), so every
incident has a page.

The CI check (`docsite check`) ties documentation to the change. A change in `aiflow/v4/control/ui/` needs the
changed screen page under `docs/guide/screens/`. For `console.html`, the check maps edits inside a view section and
JavaScript renderers such as `renderProjects` to their screen pages. Shared Help links, first-use hints and Help
plumbing map to every screen; an edit to one hint maps to that screen. The renderer map lives in
`SCREEN_RENDERER_VIEWS` in `aiflow/v4/core/docsite.py`. If a UI change cannot be mapped, the check fails until its
screen mapping is added; an unrelated screen page cannot satisfy it. A change in any control module that registers
`/v1/...` routes (found from the fully mounted app, including `api.py`, `login.py`, `jira_connect.py` and
`ui/__init__.py`) needs a changed
page in `docs/guide/` or `docs/v4/` that names one of that module's routes. An unrelated documentation edit does not
count.

The route check also follows shared response helpers. For example, `control/state.py` supplies both
`GET /v1/state` and `GET /v1/deploy/state`, so changing that snapshot helper requires a page that names both routes.
The generated API reference lists request and response JSON fields with their types and required status. The
`GET /v1/state` response shape is declared in the mounted app's OpenAPI schema. Release packaging passes its release
version to both the API reference and the site footer.

## Build

```sh
uv run --frozen python -m aiflow.v4.core.docsite build --out build/docs-site --version vX.Y.Z
```

The build fails on an unsafe link scheme or a broken relative link between pages. Relative paths, same-site root paths,
in-page anchors and external `http`, `https` and `mailto` links are accepted. In the release workflow `deploy/v4/ci/add-docs-site.sh VERSION
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

It fails when the pull request changes a mapped screen without changing its screen page, or changes a control API
route without changing a page that names that route. Unmapped UI changes fail until they are mapped. Unrelated
documentation edits do not satisfy the check.
