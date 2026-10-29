# Code quality gate

Code quality is a minimum requirement and does not depend on a third-party reviewer having quota. `core/quality_checks.py`
counts three kinds of finding and compares each total with `docs/v4/quality-baseline.json`:

- **ruff rules** (`pyproject.toml`): pyflakes, pycodestyle errors, bugbear, simplify and McCabe complexity above 12.
- **long-function**: a function longer than 80 lines.
- **layer**: an import that breaks the dependency direction of [the architecture](architecture.md) (control does not import
  worker or node; core does not import control, worker or node; adapters import none of them).

A change fails when a total grows. The baseline may only shrink, so moving a legacy function into a new module is allowed and
adding a new violation is not. Run it with `python3 -m aiflow.v4.core.quality_checks check --base origin/develop`; after you
fix findings, lower the baseline with `python3 -m aiflow.v4.core.quality_checks baseline --write` (it refuses to raise it).
CI runs the same command as the required check `Code quality`; the local gate runs it as the `quality` step.

If CodeRabbit, Qodo or any other reviewer is rate limited, paused or removed, nothing changes: this check and the verify
AI's code-quality checklist (single responsibility, extension without editing central files, no duplication, errors not
swallowed) still decide.
