# Browser E2E steps, suites and scenarios

Browser journeys in `tests/v4/browser_e2e/` are composed from three reusable layers. Each step performs one browser
action or checks one result. A suite lists its steps in order, and a scenario lists its suites in order. The log viewer
scenario shows redacted lines and then handles an empty read; both suites reuse the same navigation, source and read
steps.

To extend coverage, add a named action or result check to a step module, reference it in an ordered suite, and include
that suite in a scenario. Reuse a step by referencing the same exported step in more than one suite. The layer runner
passes the same browser context through each step, so a scenario can carry state between suites.

The existing Python harness still starts the real v4 control API and runs the scenario in the installed Playwright
browser. It uses the isolated project test database and test Redis keys; the browser journey is scripted and makes no
AI calls.
