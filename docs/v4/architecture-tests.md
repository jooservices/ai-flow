# Architecture tests on every pull request

`tests/v4/test_architecture.py` turns the rules of the [v4 control plane architecture](architecture.md) into checks
that need no database or Redis. The same rules are also covered by the more focused checks listed at the end.

The tests check:

- **Dependency direction:** control does not import worker or node; runtime core does not import control (the
  build-time documentation tool is the only exception); adapters do not import control, worker or node.
- **Route registration:** every current `install`, `mount` and `install_login` function in control is called from
  `create_app`, and feature routers under `control/routes/` are discovered by the route registry instead of a central
  list.
- **Feature and event imports:** `tests/v4/test_feature_structure.py` enforces the `routes`, `service`, `repository`,
  `models`, `events` and `listeners` boundaries of the event architecture.
- **Principles:** the architecture page, its Topics link and the developer and verification prompts stay in place,
  and the checked functions stay within the 80-line and complexity-12 budgets.
- **Documentation:** this page and the architecture page are linked from `deploy/v4/README.md`.

The `Architecture` job in `.github/workflows/ci.yml` runs the tests for every pull request and merge request, so a
failure fails the pull request check. The v4 shards run the same file again with the rest of the suite.

## Adding a rule

1. Write the rule in `docs/v4/architecture.md`, with its registration point when it has one.
2. Add a test to `tests/v4/test_architecture.py`. Read the source with `ast` so the check needs no running service;
   keep the helper and test functions within 80 lines and complexity 12.
3. Prove both outcomes: a passing real tree and a synthetic violation, following the style of
   `tests/v4/test_feature_structure.py`.
4. Run the architecture tests on their own, then the complete v4 suite.

The focused checks remain the place for a rule with its own baseline or its own implementation, such as the giant
files, duplicate blocks, broad exceptions, SQL boundary and code-quality gates.
