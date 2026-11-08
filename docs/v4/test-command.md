# Unified test command for every layer

A developer AI must never invent how to run tests. `scripts/test` provides a single command that runs every test layer
across the repository: `fast` (lint, types, guards, and unit tests), `integration` (database and services), `smoke`,
and `e2e` (browser journeys and acceptance workflows).

## Usage

```bash
scripts/test [OPTIONS] [LAYERS...] [PATHS...]
```

When run without arguments, `scripts/test` executes all four layers in sequence. Specific layers can be selected:

```bash
scripts/test fast
scripts/test integration
scripts/test smoke
scripts/test e2e
scripts/test fast integration
```

To focus on particular files while preserving layer semantics, pass the target paths:

```bash
scripts/test fast tests/v4/test_commit_rules.py
scripts/test smoke tests/v4/test_install_smoke.py
```

### Options

- `-x`, `--fail-fast`: stop execution immediately after the first failing layer.
- `--base BASE`: Git base reference for quality baseline guards (defaults to `origin/develop`, fallback to `develop` or `HEAD`).

## Layers

- **`fast`:** runs type compilation (`python3 -m compileall`), static quality checks (`quality_checks check` against
  baseline), architectural guard scripts (`giant_files`, `duplicate_blocks`, `sql_boundary`, `broad_except`), and
  isolated in-memory unit tests (`pytest -m unit`).
- **`integration`:** runs database and service integration tests (`pytest -m integration`) against PostgreSQL and Redis.
- **`smoke`:** runs rapid confidence checks (`pytest -m smoke`), including installation and publication verification.
- **`e2e`:** runs end-to-end user browser journeys and clean-install acceptance workflows (`pytest -m e2e`).

## Output and Token Efficiency

Every layer outputs a single concise status line on completion:

```text
fast: PASS
integration: PASS
smoke: PASS
e2e: PASS
```

When a layer fails, subsequent layers continue reporting by default so a complete picture is produced. A concise
failure summary (the tail of the failure output) is printed at the end of the run, keeping output short so an AI
consuming the output spends minimal tokens.

## Parity and Local Gates

The command costs no external service calls or AI tokens to execute. It runs identically in local development, Git
pre-push hooks, and CI workflows.
