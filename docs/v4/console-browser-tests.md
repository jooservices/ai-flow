# Console browser tests

`tests/v4/test_console_browser.py` starts the real v4 FastAPI control plane with the existing test Redis and
PostgreSQL services, seeds an install-provisioned Administrator plus sample projects, tickets, review items, nodes
and accounts, then runs `console_browser_suite.cjs` in headless Chromium through Playwright. The browser uses the
same login, view APIs and write paths as a person in Command Center; the test data and synthetic passwords exist
only in the isolated test database.

## Browser coverage

The suite starts from the documented first-run install state: the installer creates the first Administrator, who
then signs in through `/login` for the first time. It exercises sign-in and the role menus for all nine v4 roles,
checks direct access to a forbidden view, and opens every view available to each role. Every role menu and forbidden
view check runs in light theme at desktop width, dark theme at desktop width and light theme at a 390-pixel phone
width. The Administrator journeys use those same three browser modes.

Each visible screen runs an accessibility check for the page landmarks, accessible names on visible actions and
form controls, and a keyboard path from the selected tab into the screen. The sign-in form receives the same label
and keyboard checks. A missing landmark, unnamed control, unreachable screen or failed console journey fails the
test. Element interaction uses Playwright role and label locators.

The sample journeys cover the project picker search and starred section, Work board type/AI/text filters, Dashboard
sections, all four Human review actions, machine join command creation, AI account creation, user/role creation,
project listing and command search. Review, machine/account and user mutations run in all three Administrator
contexts against disposable test services.

The browser runner has a hard 600-second timeout and reports elapsed time on success or failure. It is part of
`uv run --frozen pytest tests/v4 -q`, so the normal regression command runs the browser suite too.

## Pull request and release workflows

The `v4` job in `.github/workflows/ci.yml` installs Playwright 1.64.1 and its Chromium browser before the v4 tests.
That job runs on every pull request, including changes to the console. The fast clean-install acceptance path also
runs the browser test directly, and the full acceptance paths include it in `tests/v4`. Each job uses its shared
test Redis and PostgreSQL services to seed the test control plane. The release `v4-tests` job installs the same
browser toolchain.

The release workflow's `deploy/v4/ci/install-smoke.sh` path also runs the browser suite after its signed candidate,
two-node join, claim, rollout and rollback checks. It runs the browser fixture against a separate test control plane
on the job's shared Redis and PostgreSQL services; no browser sample data is written into the installed candidate's
control database.

If a browser test fails, the test writes Playwright trace archives under
`AIFLOW4_PLAYWRIGHT_TRACE_DIR`. The harness removes payload resources, redacts credentials and cookies, and
redacts bearer values before the CI artifact step uploads the traces. The CI v4 and clean-install acceptance jobs,
and the release v4 test, acceptance and install smoke paths, upload traces only after a failure, for 14 days. They
contain no `/etc/aiflow` files or production credentials.

Install Chromium locally with `playwright install chromium` if the machine's Playwright toolchain does not already
provide it. The CI workflow installs Chromium with its operating-system libraries through
`playwright install chromium --with-deps`.
